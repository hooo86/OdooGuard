#!/bin/bash
# =============================================================================
# install-phase2.sh - tableau de bord, alertes e-mail/Telegram, Zabbix, Veeam/G:
#
# Prérequis : phase 1 installée (install-odooguard.sh). Idempotent.
# Usage (root, depuis la racine du dépôt) :
#   OG_IP=192.168.8.81 ./install/install-phase2.sh
# Sans accès à PyPI : WHEELHOUSE=/chemin/vers/wheels ./install/install-phase2.sh
#   (wheels préparées ailleurs avec : pip download -r requirements-web.txt -d wheels)
# =============================================================================
set -euo pipefail

OG_IP="${OG_IP:-192.168.8.81}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
ETC=/etc/odooguard
CONF="$ETC/odooguard.conf"
step() { echo; echo "==> $*"; }

[[ $EUID -eq 0 ]] || { echo "à lancer en root" >&2; exit 1; }
[[ -f "$CONF" ]] || { echo "phase 1 absente ($CONF)" >&2; exit 1; }

step "1. Code OdooGuard"
rsync -a --delete --exclude .git --exclude tests --exclude venv "$SRC/" /opt/odooguard/
cat > /usr/local/bin/odooguard-zabbix <<'EOF'
#!/bin/sh
# Lecture seule de /var/lib/odooguard-public/status.json (aucun secret, aucun réseau)
exec python3 -I /opt/odooguard/odooguard/zabbix.py "$@"
EOF
chmod 755 /usr/local/bin/odooguard-zabbix

step "2. Environnement Python du tableau de bord (/opt/odooguard/venv)"
if [[ "${SKIP_VENV:-0}" != 1 ]]; then
    apt-get install -y -q python3-venv >/dev/null
    [[ -x /opt/odooguard/venv/bin/python ]] || python3 -m venv /opt/odooguard/venv
    if [[ -n "${WHEELHOUSE:-}" ]]; then
        /opt/odooguard/venv/bin/pip install -q --no-index --find-links "$WHEELHOUSE" -r "$SRC/requirements-web.txt"
    else
        /opt/odooguard/venv/bin/pip install -q -r "$SRC/requirements-web.txt"
    fi
fi

step "3. Répertoires et secrets"
install -d -o odooguard -g odooguard -m 755 /var/lib/odooguard-public
install -d -o odooguard -g odooguard -m 700 "$ETC/web" "$ETC/tokens"
for f in smtp.pass telegram.token; do
    [[ -f "$ETC/$f" ]] || { install -o odooguard -g odooguard -m 600 /dev/null "$ETC/$f"; echo "  à remplir : $ETC/$f"; }
done
for t in veeam offline; do
    if [[ ! -s "$ETC/tokens/$t" ]]; then
        (umask 077; openssl rand -hex 32 > "$ETC/tokens/$t")
        chown odooguard:odooguard "$ETC/tokens/$t"
        echo "  jeton créé : $ETC/tokens/$t"
    fi
done

if [[ ! -s "$ETC/web/secret" ]]; then
    # Clé de signature des sessions : le service web (cloisonné, /etc en lecture
    # seule) ne peut pas la créer lui-même.
    (umask 077; openssl rand -hex 32 > "$ETC/web/secret")
    chown odooguard:odooguard "$ETC/web/secret"
    echo "  clé de session créée : $ETC/web/secret"
fi

step "4. Certificat HTTPS auto-signé (10 ans)"
if [[ ! -f "$ETC/web/odooguard.crt" ]]; then
    openssl req -x509 -newkey rsa:3072 -sha256 -days 3650 -nodes \
        -keyout "$ETC/web/odooguard.key" -out "$ETC/web/odooguard.crt" \
        -subj "/CN=$(hostname -f)/O=LNHC OdooGuard" \
        -addext "subjectAltName=DNS:$(hostname -f),DNS:$(hostname -s),IP:$OG_IP" 2>/dev/null
    chown odooguard:odooguard "$ETC/web/odooguard.key" "$ETC/web/odooguard.crt"
    chmod 600 "$ETC/web/odooguard.key"; chmod 644 "$ETC/web/odooguard.crt"
fi
THUMB=$(openssl x509 -in "$ETC/web/odooguard.crt" -noout -fingerprint -sha1 | cut -d= -f2 | tr -d :)

step "5. Configuration : ajout des sections de la phase 2 si absentes"
if ! grep -q '^\[alerts\]' "$CONF"; then
    cp -a "$CONF" "$CONF.avant-phase2"
    sed -n '/^# =* phase 2/,$p' "$SRC/conf/odooguard.conf.example" >> "$CONF"
    echo "  sections ajoutées (sauvegarde : $CONF.avant-phase2) : relire [smtp], [telegram], [external:*]"
fi

step "6. Service web"
install -m 644 "$SRC/deploy/systemd/odooguard-web.service" /etc/systemd/system/
systemctl daemon-reload 2>/dev/null || true
systemctl enable --now odooguard-web.service 2>/dev/null || echo "  (systemd absent : laboratoire)"

step "7. Zabbix (si l'agent 2 est installé)"
if [[ -d /etc/zabbix/zabbix_agent2.d ]]; then
    install -m 644 "$SRC/deploy/zabbix/userparameter_odooguard.conf" /etc/zabbix/zabbix_agent2.d/
    systemctl restart zabbix-agent2 2>/dev/null || true
    echo "  test : zabbix_agent2 -t odooguard.overall"
else
    echo "  agent 2 absent : installer zabbix-agent2 puis relancer ce script"
fi

cat <<EOF

Phase 2 installée.
  1. Créer un compte du tableau de bord :  sudo odooguard web-user add <nom>
  2. Ouvrir https://$OG_IP:8443  (certificat auto-signé, empreinte SHA-1 : $THUMB)
  3. Renseigner [smtp] / [telegram] dans $CONF et les secrets
     $ETC/smtp.pass, $ETC/telegram.token, puis :  sudo odooguard alert-test
  4. Windows (Veeam / copie G:) : copier deploy/windows/*.ps1 et les jetons
     $ETC/tokens/veeam et $ETC/tokens/offline (voir docs/installation-phase2.md),
     paramètre -CertThumbprint $THUMB
  5. Zabbix : importer deploy/zabbix/zbx_template_odooguard.yaml et le lier à l'hôte OdooGuard.
EOF
