# Installation — phase 1 (socle)

Durée estimée : une demi-journée, plus une **fenêtre de maintenance d'environ 5 min**
sur server01 (redémarrage de PostgreSQL, Odoo coupé environ 1 min).

Hypothèses (à adapter) : OdooGuard = `192.168.8.81`, VM de test = `192.168.8.82`.
Les valeurs marquées **[À VÉRIFIER]** sont à confirmer sur place.

---

## Étape 0 — Relevés préalables sur server01

```bash
pg_lsclusters
```
```bash
psql -V
```
```bash
cat /etc/debian_version
```
```bash
df -h /var/lib/postgresql /var/lib/odoo
```
```bash
grep -E '^(addons_path|data_dir|db_user|db_host)' /etc/odoo/odoo.conf
```
```bash
sudo -u postgres psql -XAtc "select name, setting from pg_settings where name in ('wal_level','archive_mode','archive_command','listen_addresses','max_wal_senders','max_replication_slots')"
```

Noter :
- la **version majeure de PostgreSQL**. Debian 12 → 15. Tous les fichiers sont écrits pour 15 ;
  les scripts acceptent `PG_VERSION=` ;
- le **chemin des modules additionnels** (`addons_path`), à reporter dans `file_sources` et
  `IMPORT_PATHS` ;
- l'**espace libre** sous `/var/lib/postgresql`. Il borne `max_slot_wal_keep_size`
  (20 Go par défaut) et doit permettre, lors d'une restauration de l'instance, de conserver
  l'ancien répertoire.

Sauvegarde de sécurité avant toute modification : laisser tourner une dernière fois
`odoo_backup.sh` et vérifier le snapshot Veeam de server01.

---

## Étape 1 — VM OdooGuard (Ubuntu Server 24.04 LTS)

1.1 Créer la VM : 4 vCPU, 8 Go de RAM, disque système de 40 Go, **second disque de 1 To**
pour les dépôts, de préférence sur un autre datastore que server01. Réseau dans le même
VLAN que server01. IP fixe 192.168.8.81.

1.2 Installer Ubuntu Server 24.04 minimal, avec OpenSSH, et activer les mises à jour
de sécurité automatiques (`unattended-upgrades`).

1.3 Formater et monter le disque des dépôts :

```bash
sudo mkfs.ext4 -L odooguard /dev/sdb
```
```bash
echo 'LABEL=odooguard /srv/odooguard ext4 defaults,noatime 0 2' | sudo tee -a /etc/fstab
```
```bash
sudo mkdir -p /srv/odooguard && sudo mount /srv/odooguard
```

1.4 Montages des NAS.

**Plus simple, après l'installation de la phase 3 :** menu **NAS** du tableau de bord
(`docs/installation-phase3.md`, section 5 bis). Les étapes ci-dessous sont la méthode
manuelle équivalente. Les identifiants vont dans un fichier 600, jamais dans fstab :

```bash
sudo install -d -m 700 /etc/odooguard
```
```bash
sudo install -m 600 /dev/null /etc/odooguard/nas1.cred
```
```bash
sudoedit /etc/odooguard/nas1.cred
```

Contenu de `nas1.cred` : `username=...`, `password=...`, `domain=...`, une clé par ligne.
Faire de même pour `nas2.cred`. Les UID et GID d'`odooguard` sont connus après l'étape 2 ;
les remplacer ci-dessous. Lignes à ajouter à `/etc/fstab` :

```
//192.168.8.30/odoo     /mnt/nas-odoo     cifs  credentials=/etc/odooguard/nas1.cred,uid=odooguard,gid=odooguard,file_mode=0640,dir_mode=0750,vers=3.0,hard,_netdev,x-systemd.automount,x-systemd.mount-timeout=30  0 0
//192.168.3.19/Backups  /mnt/nas-backups  cifs  credentials=/etc/odooguard/nas2.cred,uid=odooguard,gid=odooguard,file_mode=0640,dir_mode=0750,vers=3.0,hard,_netdev,x-systemd.automount,x-systemd.mount-timeout=30  0 0
```

`hard` fait remonter les erreurs d'E/S au lieu de les masquer : OdooGuard les détecte
(test d'écriture et relecture avant chaque copie, puis toutes les 5 min).

---

## Étape 2 — Installation d'OdooGuard

```bash
git clone <url-du-dépôt> /root/odooguard-src
```
```bash
cd /root/odooguard-src && sudo ODOO_IP=192.168.8.80 OG_IP=192.168.8.81 PG_VERSION=15 ./install/install-odooguard.sh
```

Le script :
- ajoute apt.postgresql.org ;
- installe `postgresql-15` (binaires seulement, aucune instance créée), pgBackRest,
  restic et rsync ;
- crée les comptes `odooguard` et `pgbr-reader` (lecture seule) et l'arborescence ;
- génère **une seule fois** les secrets : `restic.pass`, `pgbackrest-cipher.pass` et
  `pgpass` ;
- génère les clés SSH `id_agent` et `id_pgbackrest` ;
- installe la configuration et les unités systemd **sans les activer**.

Il affiche à la fin :
- les deux clés publiques à installer sur server01 ;
- le mot de passe du rôle de réplication, 5e champ de `/etc/odooguard/pgpass`.

**Imprimer `restic.pass` et `pgbackrest-cipher.pass` et les ranger sous pli scellé**
(coffre, avec la procédure de restauration). Sans eux, les copies NAS sont inutilisables.

Relire `/etc/odooguard/odooguard.conf` et corriger les valeurs **[À VÉRIFIER]**
(`file_sources`, cibles, NAS).

---

## Étape 3 — Préparation de server01 (sans coupure)

Copier le dépôt sur server01, puis :

```bash
sudo OG_IP=192.168.8.81 PG_VERSION=15 AGENT_PUBKEY="$(cat id_agent.pub)" PGBR_PUBKEY="$(cat id_pgbackrest.pub)" REPL_PASSWORD='<5e champ du pgpass>' ./install/install-server01.sh
```

Le script ne redémarre rien. Il :
1. ajoute apt.postgresql.org et installe pgBackRest, à la **même version** que sur OdooGuard ;
2. installe `odooguard-agent`, `odooguard-pgbr-gate`, `odooguard-export` et `/etc/odooguard-agent.conf` ;
3. ajoute les clés d'OdooGuard avec `restrict,from="192.168.8.81",command="…"` et
   `PermitRootLogin forced-commands-only` ;
4. crée le rôle `odooguard_repl` (REPLICATION, sans autre droit) et le slot `odooguard` ;
5. ajoute la ligne `pg_hba.conf` et dépose `conf.d/odooguard.conf`, puis recharge PostgreSQL ;
6. ouvre le port 5432 à OdooGuard seulement, si ufw est actif ;
7. sauvegarde chaque fichier modifié en `*.avant-odooguard`.

Il affiche la clé publique `postgres@server01` à autoriser pour `pgbr-reader` sur OdooGuard :

```bash
echo 'restrict,from="192.168.8.80,192.168.8.82",command="/usr/local/sbin/odooguard-pgbr-gate repo" ssh-ed25519 AAAA... postgres@server01-restore' | sudo tee -a /var/lib/pgbr-reader/.ssh/authorized_keys
```

Empreintes d'hôte. **Vérifier visuellement** l'empreinte affichée contre celle de la console
de server01 (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`). Sur OdooGuard :

```bash
sudo -u odooguard sh -c 'ssh-keyscan 192.168.8.80 >> ~/.ssh/known_hosts'
```

Sur server01 :

```bash
sudo -u postgres sh -c 'ssh-keyscan 192.168.8.81 >> ~/.ssh/known_hosts'
```

### Modifications exactes de la configuration PostgreSQL

Fichier ajouté : `/etc/postgresql/15/main/conf.d/odooguard.conf`, voir
`conf/postgresql/odooguard.conf`.

| Paramètre | Avant (défaut Debian) | Après | Prise en compte |
|---|---|---|---|
| `listen_addresses` | `localhost` | `localhost,192.168.8.80` | **redémarrage** |
| `wal_level` | `replica` | `replica` (explicite) | redémarrage si modifié |
| `archive_mode` | `off` | `on` | **redémarrage** |
| `archive_command` | vide | `/bin/true # pgbackrest : …` | rechargement |
| `archive_timeout` | `0` | `300` | rechargement |
| `max_wal_senders` | `10` | `10` | redémarrage si modifié |
| `max_replication_slots` | `10` | `10` | redémarrage si modifié |
| `max_slot_wal_keep_size` | `-1` | `20GB` **[À VÉRIFIER : espace libre]** | rechargement |
| `wal_compression` | `off` | `lz4` | rechargement |

Ligne ajoutée **en tête** de `pg_hba.conf` (rechargement) :

```
host    replication     odooguard_repl      192.168.8.81/32     scram-sha-256
```

---

## Étape 4 — Fenêtre de maintenance (Odoo coupé environ 1 min)

Prévenir les utilisateurs, puis sur server01 :

```bash
sudo -u postgres psql -XAtc "select name, setting from pg_settings where pending_restart"
```
```bash
sudo systemctl stop odoo
```

Facultatif, recommandé : passer PostgreSQL sur les paquets pgdg, même version majeure
(mise à jour mineure, sans conversion de données) :

```bash
sudo apt-get install -y --only-upgrade postgresql-15 postgresql-client-15
```

Redémarrage, puis contrôles :

```bash
sudo systemctl restart postgresql@15-main
```
```bash
sudo -u postgres psql -XAtc "show archive_mode; show listen_addresses"
```
```bash
sudo systemctl start odoo
```
```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8069/web/login
```

Retour arrière immédiat en cas de problème : `docs/retour-arriere-phase1.md`, section A.

---

## Étape 5 — Initialisation et première sauvegarde (sur OdooGuard)

```bash
sudo systemctl enable --now odooguard-walreceiver.service
```
```bash
sudo odooguard init
```

`init` récupère `pg_control`, crée la stanza pgBackRest et initialise restic. Démarrer
ensuite le versement des WAL et vérifier :

```bash
sudo systemctl enable --now odooguard-walingest.service
```
```bash
sudo -u odooguard pgbackrest --stanza=lnhc check
```

Première sauvegarde complète, de préférence le soir. Durée attendue : 30 à 60 min.

```bash
sudo systemctl start odooguard-backup-db@full.service
```

Puis la première sauvegarde des fichiers. Elle copie environ 60 Go, ensuite seulement
les différences.

```bash
sudo systemctl start odooguard-backup-files.service
```

Initialiser les copies NAS : la première est longue, les suivantes incrémentales.

```bash
sudo install -d -o odooguard -g odooguard /mnt/nas-odoo/odooguard /mnt/nas-backups/odooguard
```
```bash
sudo systemctl start odooguard-copy.service
```

État :

```bash
sudo odooguard status
```
```bash
sudo odooguard list
```

---

## Étape 6 — Activation des planifications

```bash
sudo systemctl enable --now odooguard-backup-db-full.timer odooguard-backup-db-diff.timer odooguard-backup-db-incr.timer odooguard-backup-files.timer odooguard-copy.timer odooguard-verify-quick.timer odooguard-verify-full.timer odooguard-maintenance.timer odooguard-status.timer
```
```bash
systemctl list-timers 'odooguard*'
```

Laisser tourner l'ancien `odoo_backup.sh` en parallèle **pendant 2 semaines** : il
reste le filet de sécurité jusqu'à la recette (`docs/tests-et-recette.md`). Le désactiver
ensuite, ou le passer en hebdomadaire.

---

## Étape 7 — Contrôles de fin d'installation

| Contrôle | Commande | Attendu |
|---|---|---|
| Flux WAL actif | `sudo odooguard status` | `wal` OK, retard < 10 s |
| Slot côté server01 | `sudo -u postgres psql -c "select slot_name, active, wal_status from pg_replication_slots"` | `active = t`, `reserved` |
| server01 ne peut pas écrire | `sudo -u pgbr-reader touch /srv/odooguard/pgbackrest/x` | *Permission denied* |
| Filtre SSH | depuis server01 : `sudo -u postgres ssh pgbr-reader@192.168.8.81 id` | refusé |
| Restauration de test | `sudo odooguard restore db --db LNHC_Prod_2025 --as LNHC_essai --target testvm --time "-1h"` | rapport JSON, base neutralisée |
| Journal d'audit | `sudo tail /var/log/odooguard/audit.log` | entrées `restore-db` |
