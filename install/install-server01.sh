#!/bin/bash
# =============================================================================
# install-server01.sh - préparation de server01 (Debian, Odoo 17) pour OdooGuard
#
# Usage (en root sur server01, depuis une copie du dépôt) :
#   OG_IP=192.168.8.81 PG_VERSION=15 \
#   AGENT_PUBKEY="ssh-ed25519 AAAA... odooguard-id_agent" \
#   PGBR_PUBKEY="ssh-ed25519 AAAA... odooguard-id_pgbackrest" \
#   REPL_PASSWORD='<5e champ de /etc/odooguard/pgpass sur OdooGuard>' \
#   ./install/install-server01.sh
#
# Ce script NE REDÉMARRE PAS PostgreSQL : il prépare tout et affiche la
# commande de redémarrage à lancer pendant la fenêtre de maintenance.
# Il sauvegarde chaque fichier modifié en <fichier>.avant-odooguard.
# =============================================================================
set -euo pipefail

OG_IP="${OG_IP:?OG_IP requis}"
PG_VERSION="${PG_VERSION:-15}"
CLUSTER="${CLUSTER:-main}"
AGENT_PUBKEY="${AGENT_PUBKEY:?AGENT_PUBKEY requis}"
PGBR_PUBKEY="${PGBR_PUBKEY:?PGBR_PUBKEY requis}"
REPL_PASSWORD="${REPL_PASSWORD:?REPL_PASSWORD requis}"
SERVER_IP="${SERVER_IP:-$(hostname -I | awk '{print $1}')}"
PGBR_CONF="${PGBR_CONF:-/etc/pgbackrest/pgbackrest.conf}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
PGETC="/etc/postgresql/$PG_VERSION/$CLUSTER"

[[ $EUID -eq 0 ]] || { echo "à lancer en root" >&2; exit 1; }
[[ -d "$PGETC" ]] || { echo "instance $PG_VERSION/$CLUSTER introuvable ($PGETC)" >&2; exit 1; }
step() { echo; echo "==> $*"; }
backup_file() { [[ -f "$1" && ! -f "$1.avant-odooguard" ]] && cp -a "$1" "$1.avant-odooguard" || true; }

step "0. État initial (à conserver dans le compte rendu d'installation)"
pg_lsclusters
psql -V
runuser -u postgres -- psql -XAtc "select name, setting, pending_restart from pg_settings where name in
  ('wal_level','archive_mode','archive_command','max_wal_senders','max_replication_slots',
   'listen_addresses','max_slot_wal_keep_size','wal_compression')"
df -h "/var/lib/postgresql/$PG_VERSION/$CLUSTER"

if [[ "${SKIP_APT:-0}" != 1 ]]; then
step "1. Dépôt apt.postgresql.org et pgBackRest (même version que sur OdooGuard)"
apt-get update -q
apt-get install -y -q postgresql-common rsync acl
if [[ ! -f /etc/apt/sources.list.d/pgdg.list && ! -f /etc/apt/sources.list.d/pgdg.sources ]]; then
    /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
fi
# Seul pgBackRest est installé ici. La mise à jour de PostgreSQL vers les
# paquets pgdg (même version majeure, mise à jour mineure) se fera lors de la
# fenêtre de maintenance, en même temps que le redémarrage (voir la doc).
apt-get install -y -q pgbackrest
echo "pgBackRest : $(pgbackrest version)  -> DOIT être identique sur OdooGuard"
fi  # SKIP_APT

step "2. Agent, filtre pgBackRest, inventaire"
install -m 755 "$SRC/server/odooguard-agent" /usr/local/sbin/odooguard-agent
install -m 755 "$SRC/server/odooguard-pgbr-gate" /usr/local/sbin/odooguard-pgbr-gate
install -m 755 "$SRC/server/odooguard-export" /usr/local/sbin/odooguard-export
[[ -f /etc/odooguard-agent.conf ]] || install -m 644 "$SRC/conf/odooguard-agent.conf.example" /etc/odooguard-agent.conf
install -d -m 750 /var/lib/odooguard-export
install -d -o postgres -g postgres -m 750 /var/log/pgbackrest
if [[ ! -f "$PGBR_CONF" ]]; then
    install -d -m 755 "$(dirname "$PGBR_CONF")"
    sed -e "s|192.168.8.81|$OG_IP|" -e "s|/15/|/$PG_VERSION/|" \
        "$SRC/conf/pgbackrest/pgbackrest.conf.server01" > "$PGBR_CONF"
    chown root:postgres "$PGBR_CONF"; chmod 640 "$PGBR_CONF"
fi

step "3. Accès SSH d'OdooGuard (commandes forcées, IP source imposée)"
install -d -m 700 /root/.ssh
touch /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys
line="restrict,from=\"$OG_IP\",command=\"/usr/local/sbin/odooguard-agent\" $AGENT_PUBKEY"
grep -qF "$AGENT_PUBKEY" /root/.ssh/authorized_keys || echo "$line" >> /root/.ssh/authorized_keys
PGHOME=$(getent passwd postgres | cut -d: -f6)
install -d -o postgres -g postgres -m 700 "$PGHOME/.ssh"
touch "$PGHOME/.ssh/authorized_keys"; chown postgres:postgres "$PGHOME/.ssh/authorized_keys"
chmod 600 "$PGHOME/.ssh/authorized_keys"
line="restrict,from=\"$OG_IP\",command=\"/usr/local/sbin/odooguard-pgbr-gate pg\" $PGBR_PUBKEY"
grep -qF "$PGBR_PUBKEY" "$PGHOME/.ssh/authorized_keys" || echo "$line" >> "$PGHOME/.ssh/authorized_keys"
# Clé de postgres@server01 vers le compte LECTURE SEULE pgbr-reader d'OdooGuard (restaurations)
[[ -f "$PGHOME/.ssh/id_ed25519" ]] || runuser -u postgres -- ssh-keygen -q -t ed25519 -N '' \
    -C "postgres@$(hostname)-restore" -f "$PGHOME/.ssh/id_ed25519"
install -m 644 "$SRC/conf/sshd-odooguard.conf" /etc/ssh/sshd_config.d/odooguard.conf
sshd -t && { systemctl reload ssh 2>/dev/null || echo "  recharger sshd manuellement"; }

step "4. PostgreSQL : rôle de réplication, pg_hba, paramètres (sans redémarrage)"
runuser -u postgres -- psql -Xq -v pw="$REPL_PASSWORD" <<'SQL'
select format('create role odooguard_repl with login replication password %L', :'pw')
 where not exists (select 1 from pg_roles where rolname = 'odooguard_repl') \gexec
select format('alter role odooguard_repl with password %L', :'pw') \gexec
SQL
backup_file "$PGETC/pg_hba.conf"
hba="host    replication     odooguard_repl      $OG_IP/32     scram-sha-256"
grep -qF "odooguard_repl" "$PGETC/pg_hba.conf" || sed -i "1i # OdooGuard : flux de réplication (pull des WAL)\n$hba" "$PGETC/pg_hba.conf"
install -d -o postgres -g postgres "$PGETC/conf.d"
grep -qE "^\s*include_dir\s*=\s*'conf.d'" "$PGETC/postgresql.conf" || {
    backup_file "$PGETC/postgresql.conf"; echo "include_dir = 'conf.d'" >> "$PGETC/postgresql.conf"; }
sed -e "s|192.168.8.80|$SERVER_IP|" "$SRC/conf/postgresql/odooguard.conf" > "$PGETC/conf.d/odooguard.conf"
chown postgres:postgres "$PGETC/conf.d/odooguard.conf"
pg_ctlcluster "$PG_VERSION" "$CLUSTER" reload
runuser -u postgres -- psql -XAtc "select pg_create_physical_replication_slot('odooguard')
  where not exists (select 1 from pg_replication_slots where slot_name = 'odooguard')" >/dev/null
echo "Slot de réplication « odooguard » : $(runuser -u postgres -- psql -XAtc "select count(*) from pg_replication_slots where slot_name='odooguard'")"

step "5. Pare-feu : 5432 accessible uniquement depuis OdooGuard (si nftables/ufw actif)"
if command -v ufw >/dev/null && ufw status | grep -q active; then
    ufw allow from "$OG_IP" to any port 5432 proto tcp
else
    echo "  pas de ufw actif : vérifier le pare-feu / l'ESXi manuellement (port 5432 <- $OG_IP seulement)"
fi

step "6. Paramètres en attente de redémarrage"
runuser -u postgres -- psql -XAtc "select name || ' = ' || setting || '  (redémarrage requis)'
  from pg_settings where pending_restart"

cat <<EOF

Préparation terminée.
  * Clé publique à autoriser sur OdooGuard pour pgbr-reader (lecture seule) :
      $(cat "$PGHOME/.ssh/id_ed25519.pub")
  * Pendant la fenêtre de maintenance (coupure Odoo ~1 min) :
      systemctl stop odoo && systemctl restart postgresql@$PG_VERSION-$CLUSTER && systemctl start odoo
  * Retour arrière : docs/retour-arriere-phase1.md
EOF
