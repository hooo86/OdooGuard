#!/bin/bash
# =============================================================================
# install-odooguard.sh - installation d'OdooGuard sur Ubuntu Server 24.04 LTS
#
# Usage (en root, depuis la racine du dépôt cloné) :
#   ODOO_IP=192.168.8.80 OG_IP=192.168.8.81 PG_VERSION=15 ./install/install-odooguard.sh
#
# Idempotent : peut être relancé sans danger (ne régénère jamais un secret
# existant, n'écrase pas une configuration existante).
# Ne démarre AUCUNE sauvegarde : voir docs/installation-phase1.md, étape 5.
# =============================================================================
set -euo pipefail

ODOO_IP="${ODOO_IP:-192.168.8.80}"
OG_IP="${OG_IP:-192.168.8.81}"
PG_VERSION="${PG_VERSION:-15}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
ETC=/etc/odooguard

[[ $EUID -eq 0 ]] || { echo "à lancer en root" >&2; exit 1; }
. /etc/os-release
[[ "$ID" == "ubuntu" && "$VERSION_ID" == "24.04" ]] || echo "ATTENTION : prévu pour Ubuntu 24.04 (trouvé $PRETTY_NAME)"

step() { echo; echo "==> $*"; }

if [[ "${SKIP_APT:-0}" != 1 ]]; then
step "1. Dépôt officiel PostgreSQL (apt.postgresql.org) : mêmes versions que server01"
apt-get update -q
apt-get install -y -q postgresql-common ca-certificates curl gnupg
if [[ ! -f /etc/apt/sources.list.d/pgdg.list && ! -f /etc/apt/sources.list.d/pgdg.sources ]]; then
    /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
fi
# Binaires serveur nécessaires pour l'instance temporaire de restauration,
# mais AUCUNE instance permanente ne doit être créée sur OdooGuard.
install -d /etc/postgresql-common
grep -q '^create_main_cluster' /etc/postgresql-common/createcluster.conf 2>/dev/null \
    && sed -i 's/^create_main_cluster.*/create_main_cluster = false/' /etc/postgresql-common/createcluster.conf \
    || echo "create_main_cluster = false" >> /etc/postgresql-common/createcluster.conf

step "2. Paquets"
apt-get install -y -q "postgresql-$PG_VERSION" "postgresql-client-$PG_VERSION" pgbackrest \
    restic rsync python3 python3-venv cifs-utils nfs-common acl e2fsprogs
systemctl disable --now postgresql >/dev/null 2>&1 || true
# Mise à jour de restic (le paquet Ubuntu peut être ancien) : restic self-update
restic self-update >/dev/null 2>&1 || true
echo "pgBackRest : $(pgbackrest version)  -> DOIT être identique sur server01"
echo "restic     : $(restic version | head -1)"
fi  # SKIP_APT (laboratoire : paquets déjà présents)

step "3. Comptes de service"
id odooguard >/dev/null 2>&1 || useradd --system --home-dir /var/lib/odooguard --create-home \
    --shell /bin/bash odooguard
# Compte en LECTURE SEULE utilisé par server01 pour restaurer (archive-get, restore)
id pgbr-reader >/dev/null 2>&1 || useradd --system --home-dir /var/lib/pgbr-reader --create-home \
    --shell /bin/bash --groups odooguard pgbr-reader
usermod -a -G odooguard pgbr-reader

step "4. Arborescence (disque des dépôts monté sur /srv/odooguard)"
mountpoint -q /srv/odooguard || echo "ATTENTION : /srv/odooguard n'est pas un point de montage dédié (voir doc, étape 1.3)"
install -d -o odooguard -g odooguard -m 750 /srv/odooguard /srv/odooguard/pgbackrest \
    /srv/odooguard/restic /srv/odooguard/mirror /srv/odooguard/staging /srv/odooguard/wal-spool
install -d -o odooguard -g odooguard -m 750 /var/lib/odooguard /var/lib/odooguard/pgshadow \
    /var/log/odooguard /var/log/pgbackrest /var/lib/odooguard/pgbackrest-spool
install -d -o pgbr-reader -g pgbr-reader -m 750 /var/log/pgbackrest-reader
install -d -o odooguard -g odooguard -m 700 "$ETC" "$ETC/ssh"
install -m 644 "$SRC/deploy/odooguard.tmpfiles" /etc/tmpfiles.d/odooguard.conf
systemd-tmpfiles --create /etc/tmpfiles.d/odooguard.conf
# Journal d'audit en ajout seul (chattr +a) : même odooguard ne peut pas le réécrire
touch /var/log/odooguard/audit.log
chown odooguard:odooguard /var/log/odooguard/audit.log
chmod 640 /var/log/odooguard/audit.log
chattr +a /var/log/odooguard/audit.log 2>/dev/null || echo "chattr +a impossible (système de fichiers ?)"

step "5. Secrets (générés une seule fois, jamais dans Git)"
gen() { [[ -s "$1" ]] || { umask 077; openssl rand -base64 48 | tr -d '\n' > "$1"; echo "  créé : $1"; }; }
gen "$ETC/restic.pass"
gen "$ETC/pgbackrest-cipher.pass"
chown odooguard:odooguard "$ETC/restic.pass" "$ETC/pgbackrest-cipher.pass"
chmod 600 "$ETC/restic.pass" "$ETC/pgbackrest-cipher.pass"
if [[ ! -s "$ETC/pgpass" ]]; then
    umask 077
    REPL_PASS=$(openssl rand -hex 24)
    echo "$ODOO_IP:5432:replication:odooguard_repl:$REPL_PASS" > "$ETC/pgpass"
    chown odooguard:odooguard "$ETC/pgpass"; chmod 600 "$ETC/pgpass"
    echo "  créé : $ETC/pgpass  (mot de passe du rôle odooguard_repl à reporter sur server01)"
fi
CIPHER=$(cat "$ETC/pgbackrest-cipher.pass")

step "6. Clés SSH (OdooGuard -> server01 uniquement)"
for k in id_agent id_pgbackrest; do
    [[ -f "$ETC/ssh/$k" ]] || ssh-keygen -q -t ed25519 -N '' -C "odooguard-$k@$(hostname)" -f "$ETC/ssh/$k"
done
chown -R odooguard:odooguard "$ETC/ssh"; chmod 600 "$ETC/ssh/"id_agent "$ETC/ssh/"id_pgbackrest
install -d -o odooguard -g odooguard -m 700 /var/lib/odooguard/.ssh
cat > /var/lib/odooguard/.ssh/config <<EOF
# pgBackRest se connecte à server01 avec cette clé (compte postgres, commande forcée)
Host $ODOO_IP
    User postgres
    IdentityFile $ETC/ssh/id_pgbackrest
    IdentitiesOnly yes
    BatchMode yes
    StrictHostKeyChecking yes
    ServerAliveInterval 30
EOF
chown odooguard:odooguard /var/lib/odooguard/.ssh/config; chmod 600 /var/lib/odooguard/.ssh/config
install -d -o pgbr-reader -g pgbr-reader -m 700 /var/lib/pgbr-reader/.ssh
touch /var/lib/pgbr-reader/.ssh/authorized_keys
chown pgbr-reader:pgbr-reader /var/lib/pgbr-reader/.ssh/authorized_keys
chmod 600 /var/lib/pgbr-reader/.ssh/authorized_keys

step "7. Configuration pgBackRest (secret injecté, droits restreints)"
inst_conf() { # $1 modèle  $2 destination  $3 propriétaire
    if [[ ! -f "$2" ]]; then
        sed -e "s|@CIPHER_PASS@|$CIPHER|" -e "s|192.168.8.80|$ODOO_IP|g" \
            -e "s|/15/|/$PG_VERSION/|g" "$1" > "$2"
        chown "$3" "$2"; chmod 640 "$2"
        echo "  installé : $2"
    fi
}
install -d -m 755 /etc/pgbackrest
inst_conf "$SRC/conf/pgbackrest/pgbackrest.conf.odooguard" /etc/pgbackrest/pgbackrest.conf root:odooguard
inst_conf "$SRC/conf/pgbackrest/odooguard-ingest.conf" /etc/pgbackrest/odooguard-ingest.conf root:odooguard
inst_conf "$SRC/conf/pgbackrest/reader.conf" /etc/pgbackrest/reader.conf pgbr-reader:pgbr-reader
chmod 600 /etc/pgbackrest/reader.conf

step "8. OdooGuard (application Python, sans dépendance externe en phase 1)"
install -d -m 755 /opt/odooguard
rsync -a --delete --exclude .git --exclude tests --exclude venv "$SRC/" /opt/odooguard/
# Version installée (affichée en bas du tableau de bord) : commit des sources
git -C "$SRC" log -1 --format='%h du %cd' --date=format:'%Y-%m-%d %H:%M' > /opt/odooguard/BUILD 2>/dev/null || true
cat > /usr/local/bin/odooguard <<'EOF'
#!/bin/sh
exec env PYTHONPATH=/opt/odooguard python3 -m odooguard "$@"
EOF
chmod 755 /usr/local/bin/odooguard
install -m 755 "$SRC/server/odooguard-pgbr-gate" /usr/local/sbin/odooguard-pgbr-gate
if [[ ! -f "$ETC/odooguard.conf" ]]; then
    sed -e "s|192.168.8.80|$ODOO_IP|g" -e "s|^pg_version = 15|pg_version = $PG_VERSION|" \
        -e "s|/postgresql/15/bin|/postgresql/$PG_VERSION/bin|" \
        "$SRC/conf/odooguard.conf.example" > "$ETC/odooguard.conf"
    chown odooguard:odooguard "$ETC/odooguard.conf"; chmod 600 "$ETC/odooguard.conf"
    echo "  installé : $ETC/odooguard.conf  (à relire : valeurs [À VÉRIFIER])"
fi
install -m 440 "$SRC/deploy/sudoers/odooguard" /etc/sudoers.d/odooguard
visudo -cf /etc/sudoers.d/odooguard

step "9. Unités systemd (installées, NON activées)"
for f in "$SRC"/deploy/systemd/*; do
    sed -e "s|192.168.8.80|$ODOO_IP|g" -e "s|/postgresql/15/|/postgresql/$PG_VERSION/|g" \
        "$f" > "/etc/systemd/system/$(basename "$f")"
done
systemctl daemon-reload 2>/dev/null || echo "  (systemd absent : laboratoire)"

cat <<EOF

Installation terminée. Étapes suivantes (docs/installation-phase1.md) :
  * clé publique de l'agent (root@server01)      : $(cat "$ETC/ssh/id_agent.pub")
  * clé publique pgBackRest (postgres@server01)  : $(cat "$ETC/ssh/id_pgbackrest.pub")
  * mot de passe du rôle odooguard_repl          : dans $ETC/pgpass (5e champ)
  * GARDER SOUS PLI SCELLÉ : $ETC/restic.pass et $ETC/pgbackrest-cipher.pass
    (sans eux, aucune restauration possible depuis les NAS).
EOF
