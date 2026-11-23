# Retour arrière — phase 1

Toutes les modifications de server01 sont **additives** : un fichier `conf.d` ajouté, une
ligne `pg_hba`, un rôle, un slot, des clés SSH à commande forcée et des scripts dans
`/usr/local/sbin`. L'installateur a sauvegardé chaque fichier modifié en
`*.avant-odooguard`. L'ancien `odoo_backup.sh` n'est pas touché et continue de fonctionner.

## A. Problème juste après le redémarrage de PostgreSQL (fenêtre de maintenance)

Symptôme : PostgreSQL ne démarre pas, ou Odoo ne se connecte plus. Sur server01 :

```bash
sudo mv /etc/postgresql/15/main/conf.d/odooguard.conf /root/odooguard.conf.desactive
```
```bash
sudo cp -a /etc/postgresql/15/main/pg_hba.conf.avant-odooguard /etc/postgresql/15/main/pg_hba.conf
```
```bash
sudo systemctl restart postgresql@15-main
```
```bash
sudo systemctl start odoo
```

Retour à l'état exact d'avant : environ 2 min.

## B. Arrêter OdooGuard sans rien désinstaller

Sur OdooGuard :

```bash
sudo systemctl disable --now 'odooguard-*.timer' odooguard-walreceiver.service odooguard-walingest.service
```

**Important** : sur server01, supprimer alors le slot de réplication, sinon il retient les
WAL jusqu'à `max_slot_wal_keep_size` (20 Go) :

```bash
sudo -u postgres psql -c "select pg_drop_replication_slot('odooguard')"
```

Les dépôts restent intacts sur `/srv/odooguard` et sur les NAS : ils restent restaurables
(voir `restauration.md`, partie B).

## C. Retirer complètement OdooGuard de server01

```bash
sudo -u postgres psql -c "select pg_drop_replication_slot('odooguard')"
```
```bash
sudo -u postgres psql -c "drop role odooguard_repl"
```
```bash
sudo rm /etc/postgresql/15/main/conf.d/odooguard.conf
```
```bash
sudo cp -a /etc/postgresql/15/main/pg_hba.conf.avant-odooguard /etc/postgresql/15/main/pg_hba.conf
```
```bash
sudo sed -i '/odooguard/d' /root/.ssh/authorized_keys /var/lib/postgresql/.ssh/authorized_keys
```
```bash
sudo rm /etc/ssh/sshd_config.d/odooguard.conf && sudo systemctl reload ssh
```
```bash
sudo rm /usr/local/sbin/odooguard-agent /usr/local/sbin/odooguard-pgbr-gate /usr/local/sbin/odooguard-export /etc/odooguard-agent.conf
```
```bash
sudo systemctl restart postgresql@15-main
```

Le redémarrage remet `archive_mode` et `listen_addresses` à leur ancienne valeur. Le faire
dans une fenêtre de maintenance, en arrêtant Odoo avant.

pgBackRest et le dépôt apt.postgresql.org peuvent rester installés. Si PostgreSQL a été
mis à jour depuis pgdg, **ne pas** revenir aux paquets Debian : simple écart de version
mineure, aucun intérêt et un risque inutile.

## D. Retirer une alerte bloquante (slot invalidé)

Si OdooGuard est resté arrêté au-delà de 20 Go de WAL, le slot est invalidé
(`wal_status = lost`) et le flux ne peut plus reprendre. Remise en route :

```bash
sudo -u postgres psql -c "select pg_drop_replication_slot('odooguard')"
```
```bash
sudo -u postgres psql -c "select pg_create_physical_replication_slot('odooguard')"
```

Puis, sur OdooGuard :

```bash
sudo systemctl restart odooguard-walreceiver
```
```bash
sudo systemctl start odooguard-backup-db@full.service
```

La restauration à un instant précis n'est pas possible dans l'intervalle manquant. Les
sauvegardes antérieures restent restaurables jusqu'à leur dernier WAL.
