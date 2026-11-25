#!/bin/bash
# =============================================================================
# web-cert.sh : (re)crée le certificat HTTPS auto-signé du tableau de bord OdooGuard
#
#   sudo OG_NAMES="odooguard.lnhc.dz" OG_IP=192.168.8.81 ./install/web-cert.sh
#
# Le certificat couvre : les noms de OG_NAMES, le nom de la machine et l'adresse IP.
# Un navigateur refuse un certificat qui ne contient pas EXACTEMENT le nom tapé dans
# la barre d'adresse, même s'il a été installé dans les autorités de confiance.
# L'ancien certificat est conservé (<fichier>.AAAAMMJJ-HHMMSS). Valable 10 ans.
#
# Après coup : réinstaller le certificat sur les postes (autorités racines de
# confiance) et mettre à jour l'empreinte (-CertThumbprint) des scripts Windows
# (Veeam), car elle change.
# =============================================================================
set -euo pipefail
ETC=/etc/odooguard
OG_IP="${OG_IP:-192.168.8.81}"
OG_NAMES="${OG_NAMES:-odooguard.lnhc.dz}"
CRT="$ETC/web/odooguard.crt"
KEY="$ETC/web/odooguard.key"
[[ $EUID -eq 0 ]] || { echo "à lancer en root (sudo)" >&2; exit 1; }
install -d -m 750 "$ETC/web"

names=()
for n in $OG_NAMES "$(hostname -f)" "$(hostname -s)"; do
    [[ -n "$n" && ! " ${names[*]} " =~ " $n " ]] && names+=("$n")
done
san=""
for n in "${names[@]}"; do san+="DNS:$n,"; done
san+="IP:$OG_IP"

ts=$(date +%Y%m%d-%H%M%S)
[[ -f "$CRT" ]] && cp -p "$CRT" "$CRT.$ts" && cp -p "$KEY" "$KEY.$ts" && echo "ancien certificat conservé : $CRT.$ts"

openssl req -x509 -newkey rsa:3072 -sha256 -days 3650 -nodes \
    -keyout "$KEY.new" -out "$CRT.new" \
    -subj "/CN=${names[0]}/O=LNHC OdooGuard" \
    -addext "subjectAltName=$san" \
    -addext "basicConstraints=critical,CA:FALSE" \
    -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth" 2>/dev/null
chown odooguard:odooguard "$KEY.new" "$CRT.new"
chmod 600 "$KEY.new"; chmod 644 "$CRT.new"
mv "$KEY.new" "$KEY"; mv "$CRT.new" "$CRT"
if systemctl is-active --quiet odooguard-web.service 2>/dev/null; then
    systemctl restart odooguard-web.service
fi
# Copie lisible pour l'installer sur les postes Windows
install -m 644 "$CRT" /tmp/odooguard.crt

echo
echo "Certificat valable pour : $san"
echo "Empreinte SHA-1 (scripts Windows, -CertThumbprint) : $(openssl x509 -in "$CRT" -noout -fingerprint -sha1 | cut -d= -f2 | tr -d :)"
echo "Copie à installer sur les postes : /tmp/odooguard.crt"
