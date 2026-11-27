# Procédures de restauration

Ce document remplace la partie restauration d'`odoo_restore.sh`. Il est conçu pour être
**imprimé** et rangé avec les secrets sous pli scellé (`restic.pass`,
`pgbackrest-cipher.pass`).

Règles communes :
- toute opération qui remplace des données exige `--force` et la saisie du nom exact. Elle
  est journalisée dans `/var/log/odooguard/audit.log` ;
- rien n'est supprimé : une base ou un répertoire remplacé est **renommé**
  `…_avant_restauration_AAAAMMJJ-HHMMSS`. Le supprimer à la main après validation ;
- les heures s'entendent en **heure locale** d'OdooGuard. Formats acceptés :
  `"2026-11-22 14:30"`, `"hier 14:30"`, `"-2h"`.

Avant toute restauration, voir ce qui est disponible :

```bash
sudo odooguard list
```

La dernière ligne indique la fenêtre de restauration à un instant précis : depuis la plus
ancienne sauvegarde complète conservée, environ 4 semaines, jusqu'au dernier WAL reçu.

---

## Partie A — Avec OdooGuard

### A1. Récupérer des données d'hier sans toucher à la production

Cas typique : une facture supprimée par erreur. On restaure une **copie neutralisée** à
côté de la production, sur la VM de test (recommandé) ou sur server01 sous un autre nom :

```bash
sudo odooguard restore db --db LNHC_Prod_2025 --as LNHC_hier_1430 --time "hier 14:30" --target testvm
```

Ce que fait OdooGuard :
1. restaure l'instance sur OdooGuard, en ne gardant que la base demandée, et rejoue les WAL
   jusqu'à l'heure demandée ;
2. transfère la base (`pg_dump` vers `pg_restore`) ;
3. réinstalle le filestore de l'instantané restic le plus proche, au plus tard à cette heure ;
4. lance `odoo neutralize` : courriels sortants et tâches planifiées désactivés ;
5. affiche un rapport : instant réellement atteint, comptages, pièces jointes lisibles.

Ouvrir ensuite `http://192.168.8.82:8069/web?db=LNHC_hier_1430` et récupérer les données,
par export et import, ou en les ressaisissant.

Durée : 30 à 60 min pour la base de production (environ 16 Go de dump).

### A2. Remettre une base de production à un instant donné

**Arrêter Odoo d'abord.** Sinon les utilisateurs continuent d'écrire dans la base qui
va être remplacée.

```bash
sudo odooguard restore db --db LNHC_Prod_2025 --time "2026-11-22 14:30" --neutralize no --force
```
→ retaper `LNHC_Prod_2025` à la demande de confirmation.

L'ancienne base est conservée sous `LNHC_Prod_2025_avant_restauration_<date>`, et l'ancien
filestore aussi. Redémarrer Odoo ; une fois l'état validé, supprimer ces copies.

### A3. Remettre toute l'instance PostgreSQL à un instant donné

Toutes les bases de server01 sont concernées. À réserver à une corruption générale.

```bash
sudo odooguard restore cluster --target odoo --time "2026-11-22 14:30" --force
```

Odoo et PostgreSQL sont arrêtés. L'ancien répertoire de données est renommé (il faut
l'espace disque correspondant), puis l'instance est restaurée et promue. OdooGuard recrée
ensuite le slot de réplication et lance une sauvegarde complète, car une nouvelle ligne
temporelle commence. Redémarrer Odoo :

```bash
sudo -u odooguard ssh -i /etc/odooguard/ssh/id_agent root@192.168.8.80 odoo-start
```

La clé d'OdooGuard n'ouvre pas de shell : l'agent n'accepte que les verbes autorisés,
dont `odoo-start`.

### A4. Récupérer des fichiers (configuration, module, pièce jointe)

Extraction **locale**, sans risque, sur OdooGuard :

```bash
sudo odooguard restore files --path /etc/odoo --time "hier 18:00" --out /srv/odooguard/staging/extraction
```

Réinstallation sur server01 (l'existant est renommé) :

```bash
sudo odooguard restore files --path /etc/odoo --push --target odoo --force
```

### A5. Reprise complète sur un Debian neuf (server01 détruit)

1. **Figer les dernières secondes de WAL**, seulement si server01 est vraiment perdu :

   ```bash
   sudo systemctl stop odooguard-walreceiver odooguard-walingest
   ```
   ```bash
   sudo odooguard wal-ingest --finalize-partial --force
   ```

2. Installer Debian 12 (même version que l'ancien server01, voir
   `/srv/odooguard/mirror/var/lib/odooguard-export/system.txt`), avec la même IP
   192.168.8.80 si possible.

3. Installer PostgreSQL 15 (pgdg) et Odoo 17 au même niveau de version, relevé dans
   `odoo-version.txt` et `dpkg-packages.tsv` de l'export :

   ```bash
   sudo apt-get install -y postgresql-common
   ```
   ```bash
   sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
   ```
   ```bash
   sudo apt-get install -y postgresql-15
   ```

   Ajouter ensuite le dépôt Odoo 17 comme sur l'ancien serveur (voir `sources.list.d`
   dans l'export), puis :

   ```bash
   sudo apt-get install -y odoo
   ```
   ```bash
   sudo systemctl stop odoo
   ```

4. Préparer le serveur avec `install-server01.sh`, comme à l'installation (étape 3).
   Autoriser la nouvelle clé `postgres` pour `pgbr-reader` et mettre à jour les
   `known_hosts` des deux côtés : la clé d'hôte a changé.

5. Depuis OdooGuard :

   ```bash
   sudo odooguard restore full --target odoo --force
   ```

   Sans `--time`, l'instance revient au dernier WAL disponible. `restore full` enchaîne :
   instance PostgreSQL, puis `/etc/odoo`, les modules et le filestore, puis le démarrage
   d'Odoo.

6. Dépendances Python des modules, si besoin :

   ```bash
   pip install -r /srv/odooguard/mirror/var/lib/odooguard-export/pip-freeze-system.txt
   ```

   Comparer avant avec la sortie de `pip freeze` sur le nouveau serveur.

7. Contrôles :
   - `http://192.168.8.80:8069/web/login` répond ;
   - connexion d'un utilisateur ;
   - ouverture d'une pièce jointe ;
   - `sudo odooguard status`.

---

## Partie B — SANS OdooGuard (outil cassé ou VM OdooGuard perdue)

OdooGuard n'est qu'un chef d'orchestre : pgBackRest et restic sont des outils standard, et
chaque copie NAS est un dépôt complet et autonome. Il faut les deux secrets du pli scellé.

### B1. La VM OdooGuard fonctionne mais la commande `odooguard` est en panne

Tout se fait sur OdooGuard, en `odooguard`. Inventaire :

```bash
sudo -u odooguard pgbackrest --stanza=lnhc info
```
```bash
sudo -u odooguard env RESTIC_PASSWORD_FILE=/etc/odooguard/restic.pass restic -r /srv/odooguard/restic snapshots
```

Restauration de l'instance sur server01, à lancer **sur server01** : sa configuration
pgBackRest pointe déjà vers OdooGuard en lecture seule.

```bash
sudo systemctl stop odoo postgresql@15-main
```
```bash
sudo mv /var/lib/postgresql/15/main /var/lib/postgresql/15/main.avant_restauration
```
```bash
sudo install -d -o postgres -g postgres -m 700 /var/lib/postgresql/15/main
```
```bash
sudo -u postgres pgbackrest --stanza=lnhc --type=time --target="2026-11-22 14:30:00+01" --target-action=promote restore
```
```bash
sudo systemctl start postgresql@15-main
```

Attendre que `select pg_is_in_recovery()` renvoie `f`, puis :

```bash
sudo systemctl start odoo
```

Filestore d'une base, restauré depuis OdooGuard puis copié sur server01 :

```bash
sudo -u odooguard env RESTIC_PASSWORD_FILE=/etc/odooguard/restic.pass restic -r /srv/odooguard/restic restore latest --target /srv/odooguard/staging/fs --include /srv/odooguard/mirror/var/lib/odoo/.local/share/Odoo/filestore/LNHC_Prod_2025
```
```bash
sudo rsync -a /srv/odooguard/staging/fs/srv/odooguard/mirror/var/lib/odoo/.local/share/Odoo/filestore/LNHC_Prod_2025/ root@192.168.8.80:/var/lib/odoo/.local/share/Odoo/filestore/LNHC_Prod_2025/
```

La clé de l'agent ne permet que les verbes autorisés : faire ce `rsync` avec un compte
administrateur. Puis, sur server01 :

```bash
sudo chown -R odoo:odoo /var/lib/odoo/.local/share/Odoo/filestore/LNHC_Prod_2025
```

### B2. La VM OdooGuard est perdue : restauration depuis un NAS

Perte de données maximale : **1 h**, la copie étant horaire.

Sur le serveur Odoo à reconstruire (Debian, PostgreSQL 15 pgdg, pgBackRest, Odoo 17
installés, voir A5, étapes 2 et 3) :

```bash
sudo apt-get install -y cifs-utils restic
```
```bash
sudo mkdir -p /mnt/nas && sudo mount -t cifs //192.168.8.30/odoo /mnt/nas -o username=<compte>,ro,vers=3.0
```

Copier le dépôt en local si le NAS est lent ou peu fiable (recommandé) :

```bash
sudo rsync -a /mnt/nas/odooguard/pgbackrest/ /var/lib/pgbackrest-repo/
```

Configuration pgBackRest temporaire. **Phrase de chiffrement : pli scellé.**

```bash
sudo tee /etc/pgbackrest/pgbackrest.conf >/dev/null <<'EOF'
[global]
repo1-path=/var/lib/pgbackrest-repo
repo1-cipher-type=aes-256-cbc
repo1-cipher-pass=<pgbackrest-cipher.pass>
[lnhc]
pg1-path=/var/lib/postgresql/15/main
EOF
```
```bash
sudo chown -R postgres:postgres /var/lib/pgbackrest-repo
```
```bash
sudo chmod 640 /etc/pgbackrest/pgbackrest.conf
```
```bash
sudo -u postgres pgbackrest --stanza=lnhc info
```
```bash
sudo systemctl stop postgresql@15-main
```
```bash
sudo -u postgres pgbackrest --stanza=lnhc --delta --type=default restore
```

Pour un instant précis, ajouter :
`--type=time --target="AAAA-MM-JJ HH:MM:SS+01" --target-action=promote`.

```bash
sudo systemctl start postgresql@15-main
```

Fichiers (filestore, `/etc/odoo`, modules). **Mot de passe restic : pli scellé.**

```bash
sudo restic -r /mnt/nas/odooguard/restic snapshots
```
```bash
sudo restic -r /mnt/nas/odooguard/restic restore latest --target /tmp/restore
```
```bash
sudo rsync -a /tmp/restore/srv/odooguard/mirror/var/lib/odoo/.local/share/Odoo/filestore/ /var/lib/odoo/.local/share/Odoo/filestore/
```
```bash
sudo rsync -a /tmp/restore/srv/odooguard/mirror/etc/odoo/ /etc/odoo/
```
```bash
sudo chown -R odoo:odoo /var/lib/odoo /etc/odoo
```
```bash
sudo systemctl start odoo
```

Si le NAS principal est inaccessible ou renvoie des erreurs d'E/S, utiliser le NAS
secondaire `//192.168.3.19/Backups` : même arborescence `odooguard/…`. En dernier
recours : la copie hors ligne G:\ (phase 3 bis) ou la restauration Veeam de la VM entière.

### B4. Depuis le disque hors ligne G:\ (tout le reste est perdu)

Le disque contient, sous `OdooGuard\`, exactement la même arborescence qu'un NAS
(`pgbackrest\`, `restic\`, `MANIFEST.*`). La procédure B2 s'applique, la source étant le disque.

Perte de données : jusqu'à la date de la copie, voir `G:\OdooGuard\DERNIERE_COPIE.txt`
(une semaine au plus avec une copie hebdomadaire).

1. Brancher le disque sur le futur serveur Odoo (Debian), ou sur n'importe quel Linux.
   Repérer la partition avec `lsblk`, puis la monter en lecture seule :

   ```bash
   sudo mkdir -p /mnt/g && sudo mount -t ntfs3 -o ro /dev/sdX1 /mnt/g
   ```

2. Copier le dépôt pgBackRest en local (le disque reste en lecture seule) :

   ```bash
   sudo rsync -a /mnt/g/OdooGuard/pgbackrest/ /var/lib/pgbackrest-repo/
   ```

3. Suivre B2 à partir de « Configuration pgBackRest temporaire ».
   Pour restic, utiliser `-r /mnt/g/OdooGuard/restic`.

Avant de commencer, on peut vérifier que la copie est complète. Cette commande liste les
fichiers absents par rapport au manifeste ; elle ne doit rien afficher :

```bash
cd /mnt/g/OdooGuard && while IFS=$'\t' read -r f n; do [ "$(stat -c %s "$f" 2>/dev/null)" = "$n" ] || echo "ABSENT/DIFFÉRENT : $f"; done < MANIFEST.files.tsv
```

Sans aucun Linux disponible, partager `G:\OdooGuard` depuis un poste Windows, en lecture
seule, et le monter en CIFS sur le serveur à reconstruire.

### B5. Réinstaller OdooGuard et reprendre les sauvegardes existantes

Cas : la VM OdooGuard est perdue ou réinstallée, mais une copie des dépôts existe (NAS1,
NAS2 ou disque G:\). Toutes les sauvegardes, avec leur historique et la restauration à un
instant précis, sont reprises. Il faut **les deux phrases secrètes du pli scellé**.

1. Ubuntu 24.04, mêmes adresse et nom que l'ancienne VM si possible, disque de 1 To pour
   `/srv/odooguard`, sources d'OdooGuard dans `/root/odooguard-src`.

2. **Avant l'installation**, remettre les deux secrets (sinon l'installation en génère de
   nouveaux, qui ne savent pas lire les anciennes sauvegardes) :

   ```bash
   sudo install -d -m 750 /etc/odooguard
   ```
   ```bash
   sudo sh -c 'umask 077; printf "%s" "<restic.pass du pli scellé>" > /etc/odooguard/restic.pass; printf "%s" "<pgbackrest-cipher.pass du pli scellé>" > /etc/odooguard/pgbackrest-cipher.pass'
   ```

3. Installer OdooGuard normalement (`install/install-odooguard.sh`, puis phases 2 et 3).

4. Monter le NAS (tableau de bord, page NAS) et recopier les dépôts sur le disque local,
   services de sauvegarde arrêtés :

   ```bash
   sudo systemctl stop 'odooguard-*.timer' odooguard-walreceiver.service
   ```
   ```bash
   sudo rsync -a --info=progress2 /mnt/odooguard/nas1/odooguard/pgbackrest/ /srv/odooguard/pgbackrest/ && sudo rsync -a --info=progress2 /mnt/odooguard/nas1/odooguard/restic/ /srv/odooguard/restic/ && sudo chown -R odooguard:odooguard /srv/odooguard/pgbackrest /srv/odooguard/restic
   ```

   Depuis le disque G:\ : brancher le disque sur un poste Windows et copier
   `G:\OdooGuard\pgbackrest` et `G:\OdooGuard\restic` vers ces mêmes dossiers.

5. Vérifier que les anciennes sauvegardes sont lisibles :

   ```bash
   sudo odooguard list all
   ```

6. Nouvelle clé SSH d'OdooGuard : la réautoriser sur server01 (et la VM de test) en
   relançant `install/install-server01.sh`, puis relancer les services :

   ```bash
   sudo systemctl start odooguard-walreceiver.service && sudo systemctl start odooguard-backup-db@full.service
   ```

   La sauvegarde complète relance la chaîne ; la planification est à réactiver dans le
   tableau de bord.

### B3. Restauration avec l'ancien format (transition)

Tant que `odoo_backup.sh` tourne en parallèle (2 semaines après la mise en service),
`odoo_restore.sh --db-only --db X --as Y --force` reste utilisable sur ses propres archives.
