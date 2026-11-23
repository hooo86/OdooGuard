# Tests et recette — phase 1

## 1. Tests automatisés

### Tests unitaires (aucune dépendance, quelques secondes)

```bash
python3 -m pytest -q
```

Ils couvrent :
- la configuration : droits 600 exigés, surcharge par l'environnement, aucun secret dans
  le dépôt ;
- l'interprétation des dates (« hier 14:30 », « -2h », ISO) ;
- la classification des erreurs (E/S, disque plein) et le test d'écriture réel ;
- les verrous ;
- l'analyse de `pgbackrest info` ;
- le versement des WAL : ordre, reprise après échec, rétention du tampon ;
- le choix de l'instantané restic ;
- la confirmation des opérations destructives et l'audit ;
- le calcul d'état : WAL en retard, NAS non monté, copie en erreur d'E/S → critique ;
- le **filtrage SSH** : 9 tentatives d'injection refusées par `odooguard-pgbr-gate`,
  9 par `odooguard-agent`.

### Test d'intégration de bout en bout (machine jetable uniquement)

```bash
sudo PG_VERSION=16 tests/integration/lab.sh
```

Il faut une machine Ubuntu 24.04 jetable (conteneur ou VM), car **le test détruit
l'instance PostgreSQL locale**. Paquets nécessaires : `postgresql-16`, `pgbackrest`,
`restic`, `rsync`, `openssh-server`, `sqlite3`, `acl`.

La même machine joue server01 et OdooGuard, et les échanges passent réellement par SSH
avec les commandes forcées. Dernier résultat : **31 contrôles OK sur 31**.

| Étape | Vérifie |
|---|---|
| 1–2 | scripts d'installation réels, `archive_mode` après redémarrage |
| 4–5 | init, flux WAL pull, complète, incrémentale, fichiers ×2, list, status |
| 6 | **restauration à un instant T** sous un autre nom : base à T (« avant » présent, « après » absent), filestore à T, rapport de contrôle, propriétaire |
| 6b | transaction de 3 s présente seulement dans le segment en cours → récupérée (**perte ≈ 0**) |
| 7 | refus sans `--force`, ancienne base renommée et non supprimée, journal d'audit |
| 8 | copie NAS vérifiée, la copie est un dépôt pgBackRest autonome, `verify`, **NAS non monté détecté**, échec historisé |
| 10 | `pgbr-reader` ne peut ni écrire ni supprimer, lit le dépôt chiffré, filtre SSH (`expire` et shell refusés) |

## 2. Recette sur l'infrastructure réelle (critères d'acceptation)

### R1 — Restauration de `LNHC_Prod_2025` à une heure donnée de la veille

1. Le jour J à une heure H (par exemple 15:12), noter une modification visible et datée :
   créer un contact « RECETTE-ODOOGUARD H ».
2. Le jour J+1, sur la VM de test :

   ```bash
   sudo odooguard restore db --db LNHC_Prod_2025 --as RECETTE_J --time "hier 15:13" --target testvm --json | tee /var/lib/odooguard/recette-R1.json
   ```

3. Vérifier :
   - le contact existe ;
   - toute modification faite après 15:13 est absente ;
   - `control.attachments_missing = 0` ;
   - `recovery_point` du rapport est ≤ 15:13 ;
   - `/web/login` répond sur la VM de test.
4. Conserver le rapport JSON et l'extrait d'`audit.log` dans le dossier de recette.

### R2 — Perte de données maximale ≤ 1 h mesurée sur une semaine

`odooguard-status.timer` mesure toutes les 5 min la perte de données courante :
- base : retard du flux WAL ;
- fichiers : âge du dernier instantané.

Au bout de 7 jours :

```bash
sudo odooguard status --rpo-report 7d
```

Attendu :
- base : **quelques secondes** ;
- fichiers : environ 35 min. Les instantanés sont faits toutes les 30 min, plus la durée
  d'un passage.

Les deux maxima doivent être ≤ 1 h. Joindre la sortie au dossier de recette.

### R3 — Échecs détectés en moins de 15 min, sans sauvegarde partielle valide

| Scénario | Comment le provoquer | Détection | Délai |
|---|---|---|---|
| NAS absent | `sudo umount -l /mnt/nas-odoo` (ou couper le partage) | `nas:nas1` critique au contrôle suivant ; `copy:nas1` en échec catégorie `mount` | ≤ 5 min |
| Erreur d'E/S | NAS en lecture seule, ou quota plein côté NAS | test d'écriture et relecture → catégorie `io` | ≤ 5 min |
| Disque plein | `fallocate -l <taille> /srv/odooguard/remplissage` jusqu'à < `min_free_gb` | `disk:repo` critique ; la sauvegarde suivante refuse de démarrer (`space`) | ≤ 5 min |
| Chaîne d'alerte en panne | `sudo odooguard alert-test` après avoir faussé `[smtp] host` et le jeton Telegram | composant `alerting` critique sur le tableau de bord et dans Zabbix | ≤ 5 min |
| Veeam en échec / muet | désactiver la tâche planifiée Veeam ou provoquer l'échec d'un travail | `ext:veeam` critique | rapport suivant, ou 26 h sans rapport |
| Sauvegarde base en erreur | `sudo systemctl stop odooguard-walingest`, puis `systemctl start odooguard-backup-db@incr` | échec `archive-timeout` → `OnFailure` → alerte | ≤ 10 min |
| Flux WAL coupé | `sudo systemctl stop odooguard-walreceiver` | `wal` critique dès 10 min de retard | ≤ 15 min |
| server01 injoignable | couper le réseau de server01 sur une VM de test | échec des sauvegardes et retard WAL | ≤ 15 min |

Pour chaque scénario :
- noter l'heure de provocation et l'heure de l'alerte. En phase 1, l'alerte est dans
  l'heure de réception de l'e-mail ou du message Telegram (l'heure exacte d'envoi figure dans
  la page « Alertes » du tableau de bord et dans `journalctl -t odooguard-alert`) ;
- vérifier avec `odooguard list` qu'aucune sauvegarde ou copie n'a été ajoutée pendant
  l'incident ;
- remettre en état et vérifier le retour à OK.

### R4 — Test de restauration hebdomadaire

`odooguard-restore-test.timer` (samedi 06:30) restaure `LNHC_Prod_2025` sur la VM de test à
« hier 16:00 ». Il contrôle :
- l'arrêt au bon instant et la fraîcheur des données ;
- les comptages comparés à la production ;
- 200 pièces jointes ;
- la neutralisation ;
- le démarrage d'Odoo et la réponse de `/web/login`.

Le rapport part par e-mail et Telegram. Critère : un rapport « RÉUSSI » reçu chaque samedi
pendant la période de recette, et consultable dans « Tests de restauration ». Les durées
du rapport fournissent la mesure du temps de reprise. Le test R1 est le même avec un
instant choisi à la main :

```bash
sudo odooguard restore-test --time "hier 15:13"
```

### R5 — Impact imperceptible pour les utilisateurs

Mesure sur une journée ouvrée complète, depuis OdooGuard :

```bash
sudo -u odooguard python3 /opt/odooguard/tools/mesure-impact.py --url http://192.168.8.80:8069/web/login --duree 10h --intervalle 10 --csv /var/lib/odooguard/impact-$(date +%F).csv
```

Le script compare le temps de réponse médian et le 95e centile **avec et sans** sauvegarde
en cours. Critère proposé : p95 pendant une sauvegarde ≤ p95 hors sauvegarde + 20 %, et
aucune erreur. Compléter sur server01 avec `vmstat 10` et `iostat -x 10`
(paquet `sysstat`) pendant une incrémentale.

Leviers si l'impact est visible :
- `process-max=1` dans `pgbackrest.conf` ;
- `rsync_bwlimit_day` plus bas ;
- incrémentales toutes les 2 h (le RPO ne dépend pas d'elles).
