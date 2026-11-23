#!/bin/bash
# =============================================================================
# Test d'intégration de bout en bout - À LANCER UNIQUEMENT SUR UNE MACHINE
# JETABLE (conteneur / VM Ubuntu 24.04), en root. DÉTRUIT l'instance
# PostgreSQL locale « main ».
#
# La même machine joue les deux rôles :
#   * « server01 » : instance PostgreSQL locale (127.0.0.1), agent, filtre SSH ;
#   * « OdooGuard » : compte odooguard, dépôts sous /srv/odooguard.
# Les échanges passent réellement par SSH avec les commandes forcées.
#
# Scénario : installation (scripts réels) -> init -> sauvegarde complète ->
# données « avant » -> instant T -> données « après » -> incrémentale ->
# fichiers -> restauration PITR de la base à T sous un autre nom (+ filestore) ->
# contrôles -> confirmation exigée -> copie NAS + NAS absent -> lecture seule.
#
# Prérequis : postgresql-16 pgbackrest restic rsync openssh-server sudo acl
# Usage : PG_VERSION=16 tests/integration/lab.sh
# =============================================================================
set -euo pipefail
PG_VERSION="${PG_VERSION:-16}"
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
DB=LNHC_Prod_2025
FS=/var/lib/odoo/.local/share/Odoo/filestore
OG="runuser -u odooguard -- /usr/local/bin/odooguard"
pass=0; fail=0
ok()   { echo "  [OK]   $*"; pass=$((pass + 1)); }
ko()   { echo "  [ÉCHEC] $*"; fail=$((fail + 1)); }
step() { echo; echo "=== $* ==="; }
psqlp() { runuser -u postgres -- psql -XAtq "$@"; }

[[ $EUID -eq 0 ]] || { echo "root requis"; exit 1; }

step "0. Remise à zéro du laboratoire"
pkill -f pg_receivewal || true; pkill -f "odooguard wal-ingest" || true
for d in /srv/odooguard/staging/*/pgdata; do
  [[ -f "$d/postmaster.pid" ]] && runuser -u odooguard -- "/usr/lib/postgresql/$PG_VERSION/bin/pg_ctl" -D "$d" -m immediate stop || true
done
pg_dropcluster --stop "$PG_VERSION" main 2>/dev/null || true
pg_createcluster "$PG_VERSION" main >/dev/null
rm -rf /srv/odooguard /var/lib/odooguard /etc/odooguard /etc/pgbackrest /var/log/pgbackrest* \
       /var/lib/odooguard-export /var/lib/odoo /tmp/pgbackrest* /mnt/fake-nas
chattr -a /var/log/odooguard/audit.log 2>/dev/null || true; rm -rf /var/log/odooguard
rm -f /var/lib/postgresql/.ssh/authorized_keys /root/.ssh/authorized_keys
id odoo >/dev/null 2>&1 || useradd --system --home-dir /var/lib/odoo odoo
mkdir -p /run/sshd /etc/odoo; echo "[options]" > /etc/odoo/odoo.conf
pgrep -x sshd >/dev/null || /usr/sbin/sshd

step "1. Installation OdooGuard (script réel, sans apt)"
SKIP_APT=1 ODOO_IP=127.0.0.1 OG_IP=127.0.0.1 PG_VERSION="$PG_VERSION" "$SRC/install/install-odooguard.sh" >/tmp/lab-install-og.log
ok "install-odooguard.sh"
# Labo : server01 et OdooGuard partagent /etc/pgbackrest -> fichiers distincts côté « server01 »
sed -i 's|^pg1-host-config=.*|pg1-host-config=/etc/pgbackrest/server01.conf|' /etc/pgbackrest/pgbackrest.conf
REPL=$(cut -d: -f5 /etc/odooguard/pgpass)

step "2. Préparation de « server01 » (script réel, sans apt)"
pg_ctlcluster "$PG_VERSION" main start
SKIP_APT=1 OG_IP=127.0.0.1 SERVER_IP=127.0.0.1 PG_VERSION="$PG_VERSION" PGBR_CONF=/etc/pgbackrest/server01.conf \
  AGENT_PUBKEY="$(cat /etc/odooguard/ssh/id_agent.pub)" PGBR_PUBKEY="$(cat /etc/odooguard/ssh/id_pgbackrest.pub)" \
  REPL_PASSWORD="$REPL" "$SRC/install/install-server01.sh" >/tmp/lab-install-s01.log
sed -i -e 's|^log-path=.*|log-path=/var/log/pgbackrest-server01|' -e '/^\[global\]/a lock-path=/tmp/pgbackrest-server01' /etc/pgbackrest/server01.conf
install -d -o postgres -g postgres /var/log/pgbackrest-server01
chown odooguard:odooguard /var/log/pgbackrest   # labo : répertoire partagé par les deux « machines »
# Labo : seules ces sources existent sur la machine
python3 - <<'PY'
import re
p = "/etc/odooguard/odooguard.conf"
s = open(p).read()
s = re.sub(r"file_sources =\n(    .*\n)+",
           "file_sources = /var/lib/odoo/.local/share/Odoo/filestore, /etc/odoo, /var/lib/odooguard-export\n", s)
open(p, "w").write(s)
PY
cat > /etc/odooguard-agent.conf <<EOF
FILESTORE=$FS
IMPORT_PATHS="/etc/odoo:odoo:odoo"
EOF
pkill -HUP -x sshd || true
# Redémarrage exigé par archive_mode / listen_addresses (fenêtre de maintenance)
pg_ctlcluster "$PG_VERSION" main restart
[[ "$(psqlp -c 'show archive_mode')" == "on" ]] && ok "archive_mode=on après redémarrage" || ko "archive_mode"
# Clé de restauration postgres -> pgbr-reader (lecture seule)
echo "restrict,from=\"127.0.0.1\",command=\"/usr/local/sbin/odooguard-pgbr-gate repo\" $(cat /var/lib/postgresql/.ssh/id_ed25519.pub)" \
  > /var/lib/pgbr-reader/.ssh/authorized_keys
# Empreintes d'hôte connues (en production : vérifier l'empreinte !)
ssh-keyscan 127.0.0.1 2>/dev/null > /tmp/kh
install -o odooguard -g odooguard -m 600 /tmp/kh /var/lib/odooguard/.ssh/known_hosts
install -o postgres -g postgres -m 600 /tmp/kh /var/lib/postgresql/.ssh/known_hosts

step "3. Données de type Odoo"
psqlp -c "create role odoo login" 2>/dev/null || true
runuser -u postgres -- createdb -O odoo "$DB"
psqlp -d "$DB" <<'SQL'
create table res_partner(id serial primary key, name text, write_date timestamp default now());
create table res_users(id serial); create table res_company(id serial);
create table ir_attachment(id serial, store_fname text);
create table ir_config_parameter(key text, value text);
create table ir_module_module(state text);
insert into res_company default values; insert into res_users default values;
insert into ir_module_module values ('installed');
insert into ir_attachment(store_fname) values ('ab/ab001');
insert into res_partner(name) values ('initial');
alter table res_partner owner to odoo;
SQL
install -d -o odoo -g odoo -m 750 "$FS/$DB/ab"
echo "pièce jointe 1" > "$FS/$DB/ab/ab001"; chown -R odoo:odoo "$FS"

step "4. Initialisation et flux WAL (pull)"
$OG init && ok "odooguard init (pg_control via agent, stanza, restic)" || ko "init"
runuser -u odooguard -- env PGPASSFILE=/etc/odooguard/pgpass nohup \
  "/usr/lib/postgresql/$PG_VERSION/bin/pg_receivewal" \
  --dbname="host=127.0.0.1 port=5432 user=odooguard_repl application_name=odooguard" \
  --slot=odooguard --directory=/srv/odooguard/wal-spool --synchronous --status-interval=10 --no-password \
  >/tmp/lab-receivewal.log 2>&1 &
runuser -u odooguard -- nohup /usr/local/bin/odooguard wal-ingest >/tmp/lab-ingest.log 2>&1 &
sleep 4

step "5. Sauvegardes"
$OG backup db --type full && ok "sauvegarde complète" || ko "sauvegarde complète"
$OG backup files && ok "sauvegarde fichiers n°1 (pièce jointe 1)" || ko "fichiers 1"
psqlp -d "$DB" -c "insert into res_partner(name) values ('avant')"
sleep 2; T=$(date '+%Y-%m-%d %H:%M:%S'); sleep 2
psqlp -d "$DB" -c "insert into res_partner(name) values ('apres')"
psqlp -d "$DB" -c "insert into ir_attachment(store_fname) values ('cd/cd002')"
install -d -o odoo -g odoo "$FS/$DB/cd"; echo "pièce jointe 2" > "$FS/$DB/cd/cd002"
psqlp -c "select pg_switch_wal()" >/dev/null; sleep 3
$OG backup db --type incr && ok "sauvegarde incrémentale" || ko "incrémentale"
$OG backup files && ok "sauvegarde fichiers n°2 (pièce jointe 2)" || ko "fichiers 2"
$OG list all >/tmp/lab-list.txt && ok "odooguard list" || ko "list"
$OG status >/tmp/lab-status.txt; cat /tmp/lab-status.txt
grep -q "wal" /tmp/lab-status.txt && ok "odooguard status" || ko "status"

step "6. Restauration PITR de $DB à T=$T sous le nom LNHC_verif"
if $OG restore db --db "$DB" --as LNHC_verif --time "$T" --neutralize no --json >/tmp/lab-restore.json; then
  ok "restore db"
else
  ko "restore db"; cat /tmp/lab-restore.json || true
fi
rows=$(psqlp -d LNHC_verif -c "select string_agg(name, ',' order by id) from res_partner" 2>/dev/null || echo "?")
[[ "$rows" == "initial,avant" ]] && ok "contenu à l'instant T : $rows" || ko "contenu restauré : $rows (attendu initial,avant)"
[[ -f "$FS/LNHC_verif/ab/ab001" && ! -e "$FS/LNHC_verif/cd/cd002" ]] \
  && ok "filestore à l'instant T (pièce 1 présente, pièce 2 absente)" || ko "filestore restauré"
python3 -c "import json;r=json.load(open('/tmp/lab-restore.json'));c=r['control'];assert c['attachments_missing']==0 and c['partners']==2,c" \
  && ok "rapport de contrôle (comptages, pièces jointes lisibles)" || ko "rapport de contrôle"
[[ "$(stat -c %U "$FS/LNHC_verif/ab/ab001")" == "odoo" ]] && ok "propriétaire odoo" || ko "propriétaire"

step "6b. Dernières secondes : transaction présente seulement dans le segment en cours (.partial)"
psqlp -d "$DB" -c "insert into res_partner(name) values ('recent')"; sleep 3
$OG restore db --db "$DB" --as LNHC_recent --neutralize no --no-filestore >/dev/null \
  && ok "restauration au dernier état" || ko "restauration au dernier état"
last=$(psqlp -d LNHC_recent -c "select name from res_partner order by id desc limit 1" 2>/dev/null || echo "?")
[[ "$last" == "recent" ]] && ok "transaction de 3 s récupérée (perte ≈ 0)" || ko "dernière transaction : $last"

step "7. Opération destructive : confirmation exigée"
if $OG restore db --db "$DB" --as LNHC_verif --neutralize no </dev/null 2>/tmp/lab-err.txt; then
  ko "remplacement sans --force accepté !"
else grep -q -- "--force" /tmp/lab-err.txt && ok "refus sans --force" || ko "message de refus"; fi
$OG restore db --db "$DB" --as LNHC_verif --neutralize no --force --confirm LNHC_verif >/dev/null \
  && ok "remplacement confirmé" || ko "remplacement confirmé"
n=$(psqlp -c "select count(*) from pg_database where datname like 'LNHC_verif_avant_restauration_%'" || echo "?")
[[ "$n" == "1" ]] && ok "ancienne base conservée (renommée, non supprimée)" || ko "ancienne base : $n"
rows=$(psqlp -d LNHC_verif -c "select count(*) from res_partner" 2>/dev/null || echo "?")
[[ "$rows" == "4" ]] && ok "restauration « dernier état » : 4 lignes" || ko "dernier état : $rows"
grep -q '"action": "restore-db"' /var/log/odooguard/audit.log && ok "journal d'audit" || ko "audit"

step "8. Copie vers NAS (répertoire simulé) et NAS absent"
install -d -o odooguard -g odooguard /mnt/fake-nas/odooguard
cat >> /etc/odooguard/odooguard.conf <<'EOF'

[destination:fakenas]
path = /mnt/fake-nas/odooguard
min_free_gb = 0
EOF
sed -i 's/^\[destination:nas1\]/[destination:nas1]\nenabled = no/; s/^\[destination:nas2\]/[destination:nas2]\nenabled = no/' /etc/odooguard/odooguard.conf
$OG backup copy --dest fakenas && ok "copie vérifiée vers le NAS" || ko "copie NAS"
pgbackrest --config=/etc/pgbackrest/odooguard-ingest.conf --stanza=lnhc --repo1-path=/mnt/fake-nas/odooguard/pgbackrest info >/dev/null \
  && ok "la copie NAS est un dépôt pgBackRest autonome" || ko "copie NAS illisible"
step "8b. Vérification (dépôts locaux + NAS)"
$OG verify --level quick >/dev/null && ok "verify quick" || ko "verify"

sed -i 's|^path = /mnt/fake-nas/odooguard|path = /mnt/fake-nas/odooguard\nmountpoint = /mnt/fake-nas|' /etc/odooguard/odooguard.conf
set +e; $OG backup copy --dest fakenas 2>/tmp/lab-err.txt; rc=$?; set -e
[[ $rc -ne 0 ]] && grep -q "n'est pas monté" /tmp/lab-err.txt && ok "NAS non monté détecté (code $rc)" || ko "NAS absent non détecté"
last=$(sqlite3 /var/lib/odooguard/history.db "select status||':'||category from runs where component='copy:fakenas' order by id desc limit 1")
[[ "$last" == "failed:mount" ]] && ok "échec historisé (aucune copie considérée valide)" || ko "historique : $last"

step "10. Modèle pull : server01 ne peut ni écrire ni supprimer"
if runuser -u pgbr-reader -- touch /srv/odooguard/pgbackrest/archive/x 2>/dev/null; then ko "pgbr-reader peut écrire !"; else ok "pgbr-reader : écriture refusée"; fi
if runuser -u pgbr-reader -- rm -f /srv/odooguard/pgbackrest/backup/lnhc/backup.info 2>/dev/null \
   && [[ ! -f /srv/odooguard/pgbackrest/backup/lnhc/backup.info ]]; then ko "pgbr-reader peut supprimer !"; else ok "pgbr-reader : suppression refusée"; fi
runuser -u postgres -- pgbackrest --config=/etc/pgbackrest/server01.conf --stanza=lnhc info >/tmp/lab-reader-info.txt 2>&1 \
  && grep -q "full backup" /tmp/lab-reader-info.txt && ok "server01 lit le dépôt chiffré via pgbr-reader" || { ko "lecture via pgbr-reader"; cat /tmp/lab-reader-info.txt; }
if runuser -u postgres -- ssh -o BatchMode=yes pgbr-reader@127.0.0.1 "pgbackrest --stanza=lnhc --remote-type=repo expire:remote" 2>/dev/null; then
  ko "commande expire acceptée"; else ok "filtre : expire refusé à server01"; fi
if runuser -u postgres -- ssh -o BatchMode=yes pgbr-reader@127.0.0.1 "id" 2>/dev/null; then
  ko "shell accessible"; else ok "filtre : aucun shell pour server01"; fi

echo; echo "Résultat : $pass OK, $fail échec(s)"
[[ $fail -eq 0 ]]
