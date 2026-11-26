#!/bin/bash
# =============================================================================
# install-phase3.sh - assistant de restauration web + test de restauration
# hebdomadaire. Prérequis : phases 1 et 2 installées. Idempotent.
# Usage (root, racine du dépôt) : ./install/install-phase3.sh
# La VM de test se prépare à part : docs/installation-phase3.md, section 2.
# =============================================================================
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
CONF=/etc/odooguard/odooguard.conf
step() { echo; echo "==> $*"; }
[[ $EUID -eq 0 ]] || { echo "à lancer en root" >&2; exit 1; }
[[ -x /opt/odooguard/venv/bin/python ]] || [[ "${SKIP_VENV:-0}" == 1 ]] || { echo "phase 2 absente" >&2; exit 1; }

step "1. Code OdooGuard"
rsync -a --delete --exclude .git --exclude tests --exclude venv "$SRC/" /opt/odooguard/
# Version installée (affichée en bas du tableau de bord) : commit des sources
git -C "$SRC" log -1 --format='%h du %cd' --date=format:'%Y-%m-%d %H:%M' > /opt/odooguard/BUILD 2>/dev/null || true
install -d -o odooguard -g odooguard -m 750 /var/lib/odooguard/jobs /var/lib/odooguard/reports

step "1 bis. Gestion des NAS depuis le tableau de bord (programme root à liste blanche)"
install -m 755 "$SRC/server/odooguard-nas-helper" /usr/local/sbin/odooguard-nas-helper
install -m 755 "$SRC/server/odooguard-schedule-helper" /usr/local/sbin/odooguard-schedule-helper
install -d -m 755 /mnt/odooguard
install -m 440 "$SRC/deploy/sudoers/odooguard" /etc/sudoers.d/odooguard.new
if visudo -cf /etc/sudoers.d/odooguard.new >/dev/null; then
    mv /etc/sudoers.d/odooguard.new /etc/sudoers.d/odooguard
else
    rm -f /etc/sudoers.d/odooguard.new; echo "  ERREUR : règle sudoers invalide, non installée" >&2; exit 1
fi
command -v mount.cifs >/dev/null || apt-get install -y -q cifs-utils >/dev/null 2>&1 || true

step "2. Configuration : section [restore_test] si absente"
if ! grep -q '^\[restore_test\]' "$CONF"; then
    cp -a "$CONF" "$CONF.avant-phase3"
    sed -n '/^# =* phase 3/,$p' "$SRC/conf/odooguard.conf.example" >> "$CONF"
    echo "  section ajoutée (sauvegarde : $CONF.avant-phase3) : vérifier [target:testvm] et [restore_test]"
fi

step "3. Unités systemd"
for u in odooguard-jobs.service odooguard-restore-test.service odooguard-restore-test.timer; do
    install -m 644 "$SRC/deploy/systemd/$u" /etc/systemd/system/
done
if systemctl daemon-reload 2>/dev/null; then
    systemctl restart odooguard-web.service || true
    systemctl enable odooguard-jobs.service && systemctl restart odooguard-jobs.service
    echo "  odooguard-restore-test.timer installé mais NON activé :"
    echo "  l'activer après un premier test manuel réussi (docs/installation-phase3.md, étape 4)."
else
    echo "  (systemd absent : laboratoire)"
fi

cat <<'EOT'

Phase 3 installée. Suite :
  1. Préparer la VM de test (agent, ALLOW_TEST_DROP=yes, Odoo, pare-feu) : docs/installation-phase3.md
  2. Donner les rôles :  sudo odooguard web-user role <nom> operateur|admin
  3. Premier test manuel : sudo odooguard restore-test
  4. Puis : sudo systemctl enable --now odooguard-restore-test.timer
EOT
