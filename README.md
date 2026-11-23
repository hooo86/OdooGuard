# OdooGuard

Sauvegarde et restauration de l'Odoo 17 de production du LNHC (server01, Debian), pilotées
depuis une VM Ubuntu 24.04 dédiée, en **modèle « pull »** : server01 n'a aucun droit
d'écriture ni de suppression sur les sauvegardes.

| | Avant (`odoo_backup.sh`) | OdooGuard phase 1 |
|---|---|---|
| Perte de données maximale | 24 h | **quelques secondes** (flux WAL continu) ; ≤ 5 min dans le dépôt ; ≤ 1 h sur les NAS |
| Base | `pg_dump` complet, environ 2 h | pgBackRest : complète hebdo, différentielle nocturne, incrémentale horaire 8h–18h, WAL en continu |
| Fichiers | `tar.gz` complet | rsync pull + restic (incrémental, dédupliqué, chiffré), toutes les 30 min |
| Restauration à un instant précis | non | oui, à la seconde, sur environ 4 semaines |
| Stockage | CIFS (NAS déjà en erreur d'E/S) | disque local + 2 NAS en copie horaire **vérifiée** |
| Rançongiciel sur server01 | peut effacer les sauvegardes | ne peut ni écrire ni supprimer |

## Commandes

```
odooguard backup db --type full|diff|incr     sauvegarde PostgreSQL (pgBackRest)
odooguard backup files                        filestore, modules, configuration (rsync + restic)
odooguard backup copy [--dest nas1]           copie vérifiée vers les NAS
odooguard restore db --db X [--as Y] [--time T] [--target testvm] [--neutralize auto|yes|no]
odooguard restore cluster --target odoo [--time T] --force
odooguard restore files --path /etc/odoo [--time T] [--out DIR | --push --target odoo --force]
odooguard restore full --target odoo [--time T] --force     reprise complète (Debian neuf)
odooguard list [db|files|runs|all] [--json]
odooguard verify [--level quick|full] [--dest nas1]
odooguard status [--json] [--check] [--record] [--rpo-report 7d]
odooguard wal-ingest [--once|--refresh-shadow|--finalize-partial]
odooguard init | maintenance
odooguard web | web-user add|remove|list <nom>     tableau de bord (phase 2)
odooguard alert-test [--level critical]            message de test sur tous les canaux
odooguard restore-test [--db X] [--time T]         test de restauration complet + rapport (phase 3)
odooguard web-user role <nom> lecteur|operateur|admin
odooguard jobs-worker                              (service) exécute les demandes de l'assistant web
odooguard report <veeam|offline> --status success|warning|failed [--message ...]
odooguard-zabbix discovery|overall|age|rpo db|files|component <nom> <champ>
```

`T` accepte `"2026-11-22 14:30"`, `"hier 14:30"` ou `"-2h"`. Toute opération destructive
exige `--force` et la saisie du nom exact (ou `--confirm`). Elle est journalisée dans
`/var/log/odooguard/audit.log`, en ajout seul.

## Documentation

| Document | Contenu |
|---|---|
| [docs/architecture.md](docs/architecture.md) | flux, modèle pull, choix du disque local vs NFS, planification, dimensionnement |
| [docs/installation-phase1.md](docs/installation-phase1.md) | installation pas à pas, **modifications exactes de `postgresql.conf`**, redémarrage |
| [docs/restauration.md](docs/restauration.md) | procédures avec **et sans** OdooGuard, à imprimer |
| [docs/retour-arriere-phase1.md](docs/retour-arriere-phase1.md) | retour arrière |
| [docs/tests-et-recette.md](docs/tests-et-recette.md) | tests automatisés et recette des critères d'acceptation |
| [docs/installation-phase3bis.md](docs/installation-phase3bis.md) | copie hors ligne vers G:\, rotation des disques |
| [docs/installation-phase3.md](docs/installation-phase3.md) | VM de test isolée, test de restauration hebdomadaire, assistant de restauration web, rôles |
| [docs/installation-phase2.md](docs/installation-phase2.md) | tableau de bord, alertes e-mail et Telegram, Zabbix, Veeam et copie G:\, retour arrière |

## Arborescence

```
odooguard/            application Python (bibliothèque standard uniquement en phase 1)
  cli.py              interface en ligne de commande
  pg.py               pgBackRest, versement des WAL, état du flux
  files.py            rsync (miroir) + restic
  dest.py             copies NAS : montage, test d'écriture et relecture, copie, vérification
  restore.py          restaurations (instance temporaire, transfert, neutralisation)
  restoretest.py      test de restauration hebdomadaire et son rapport
  jobs.py             file des demandes web, rôles, re-validation, exécution
  status.py           état global, mesure de la perte de données
  notify.py           alertes : journal, e-mail, Telegram, anti-rafale
  external.py         rapports Veeam / copie G:\
  zabbix.py           indicateurs Zabbix (autonome, sans secret)
  web/                tableau de bord : app.py, auth.py, charts.py, templates/, static/
  history.py audit.py config.py util.py remote.py
server/               installé sur server01 et la VM de test
  odooguard-agent       agent root à liste blanche (commande forcée SSH)
  odooguard-pgbr-gate   filtre SSH : protocole pgBackRest uniquement
  odooguard-export      inventaire (paquets, pip, versions) pour reconstruire server01
conf/                 modèles de configuration (aucun secret)
deploy/systemd/       services et timers
install/              scripts d'installation OdooGuard et server01
tests/                tests unitaires + tests/integration/lab.sh (bout en bout)
deploy/zabbix/        UserParameter + modèle Zabbix
deploy/windows/       PowerShell : copie hors ligne G:\, tâche planifiée, Veeam, rapport générique
tools/mesure-impact.py  mesure de l'impact sur le temps de réponse d'Odoo
```

## Secrets

Aucun secret dans Git. Ils sont générés à l'installation dans `/etc/odooguard/` (droits 600) :
`restic.pass`, `pgbackrest-cipher.pass`, `pgpass`, clés SSH. Les deux premiers doivent aussi
être conservés **sous pli scellé** : sans eux, les copies NAS sont illisibles.

## Feuille de route

- **Phase 1 (livrée)** : socle pgBackRest + WAL pull + restic + CLI + procédures.
- **Phase 2 (livrée)** : tableau de bord web HTTPS (FastAPI, Jinja2, htmx servi en local),
  alertes e-mail et Telegram sans rafale, intégration Zabbix (modèle + découverte),
  état Veeam et copie hors ligne via rapports PowerShell.
- **Phase 3 (livrée)** : test de restauration hebdomadaire automatique sur la VM de test
  (instant précis, neutralisation, comptages comparés à la production, pièces jointes,
  /web/login), rapport envoyé ; assistant de restauration web avec rôles, confirmation
  et audit, exécuté par le service odooguard-jobs.
- **Phase 3 bis (livrée)** : copie hors ligne vers G:\ (PowerShell) : contrôle du disque,
  bascule NAS secondaire, copie robocopy ordonnée, vérification fichier par fichier
  contre le manifeste écrit par OdooGuard, disque mis hors ligne, rapport à OdooGuard.
