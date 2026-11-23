# OdooGuard — architecture (phase 1)

## 1. Vue d'ensemble

```
                       server01 (Debian, 192.168.8.80)                 OdooGuard (Ubuntu 24.04, 192.168.8.81)
                 ┌───────────────────────────────────────┐        ┌──────────────────────────────────────────────┐
                 │ Odoo 17 ── PostgreSQL 15 (pgdg)       │        │                                              │
                 │                │                      │  (1)   │ pg_receivewal ─► /srv/odooguard/wal-spool    │
                 │                └─ flux de réplication ◄────────┤   (slot « odooguard », --synchronous)        │
                 │                   (rôle odooguard_repl)│        │        │ odooguard wal-ingest (archive-push)│
                 │                                       │        │        ▼                                     │
                 │ postgres : clé forcée                 │  (2)   │ pgBackRest ─► /srv/odooguard/pgbackrest      │
                 │   odooguard-pgbr-gate pg  ◄────────────────────┤  full / diff / incr (pull SSH)  [chiffré]    │
                 │                                       │        │                                              │
                 │ root : clé forcée                     │  (3)   │ rsync ─► /srv/odooguard/mirror               │
                 │   odooguard-agent  (rrsync -ro)  ◄─────────────┤ restic ─► /srv/odooguard/restic   [chiffré]  │
                 │                                       │        │                                              │
                 │ restaurations : postgres ─────────────────────►│ pgbr-reader (LECTURE SEULE, groupe r-x)     │
                 └───────────────────────────────────────┘  (4)   │                                              │
                                                                  │ odooguard backup copy (horaire, vérifié)     │
                                                                  └──────┬───────────────────────┬───────────────┘
                                                                         ▼                       ▼
                                                              NAS 192.168.8.30 (CIFS)   NAS 192.168.3.19 (Backups)
                                                              odooguard/pgbackrest      odooguard/pgbackrest
                                                              odooguard/restic          odooguard/restic
```

Flux (toutes les connexions sont **initiées par OdooGuard**, sauf (4)) :

1. **WAL en continu** : `pg_receivewal` reçoit le flux de réplication de server01 et
   l'écrit, synchronisé sur disque, dans un tampon local. `odooguard wal-ingest` verse
   chaque segment terminé dans le dépôt pgBackRest (`archive-push` local).
2. **Sauvegardes physiques** : pgBackRest, lancé sur OdooGuard, lit les fichiers de
   PostgreSQL via SSH (`postgres@server01`, commande forcée `odooguard-pgbr-gate pg`).
3. **Fichiers** : rsync tire le filestore, les modules et la configuration (agent :
   `rrsync -ro`, lecture seule) dans un miroir ; restic en fait un instantané dédupliqué
   et chiffré.
4. **Restauration** : server01 (ou la VM de test) lit le dépôt via le compte
   `pgbr-reader`, qui n'a **aucun droit d'écriture** sur le dépôt.

## 2. Modèle « pull » strict : pourquoi les WAL ne sont pas poussés

La configuration classique de pgBackRest avec un hôte de dépôt fait pousser les WAL par
server01 (`archive_command = pgbackrest archive-push`) via SSH vers l'hôte du dépôt.
Le protocole distant de pgBackRest permet alors à server01 d'écrire, et en pratique
de supprimer, des fichiers du dépôt. Cela contredit l'exigence « server01 n'a
aucun droit d'écriture ni de suppression sur les sauvegardes » : un rançongiciel sur
server01 pourrait détruire les sauvegardes.

OdooGuard inverse donc le sens :

| Élément | Mécanisme | Droits de server01 sur OdooGuard |
|---|---|---|
| WAL | `pg_receivewal` (OdooGuard) + slot de réplication + `archive-push` **local** | aucun |
| Sauvegardes full/diff/incr | pgBackRest sur OdooGuard, lecture via SSH | aucun |
| Fichiers | rsync pull + `rrsync -ro` | aucun |
| Restauration | compte `pgbr-reader`, groupe en lecture seule | **lecture** du dépôt (chiffré) |

Détails validés en laboratoire (`tests/integration/lab.sh`, 31 contrôles) :

* pgBackRest exige `archive_mode=on` et un `archive_command` contenant « pgbackrest ».
  On utilise `archive_command = '/bin/true # pgbackrest : …'` : la commande réussit
  toujours, PostgreSQL recycle ses WAL normalement, et c'est le **slot** qui retient les
  WAL non encore reçus, avec la borne `max_slot_wal_keep_size` pour protéger le disque de server01.
* `archive-push` vérifie l'identifiant système de l'instance : OdooGuard garde une copie
  de `pg_control` (`/var/lib/odooguard/pgshadow`), récupérée en lecture via l'agent.
* `archive_timeout = 300` force une bascule de segment au plus toutes les 5 min : le
  dépôt n'a jamais plus de 5 min de retard, même quand Odoo est peu actif. Le segment en
  cours (`.partial`) est de toute façon déjà sur OdooGuard. Les restaurations faites
  sur OdooGuard (`restore db`) l'utilisent : **perte ≈ quelques secondes**, vérifiée en labo.
* `pgbr-reader` est membre du groupe `odooguard`. Les fichiers du dépôt sont en
  0640/0750, donc ce compte n'a jamais le droit w. Même en contournant le filtre SSH, il ne
  peut ni écrire ni supprimer (vérifié en labo). Le filtre n'autorise en plus que
  `restore`, `archive-get`, `info` et `verify`.

**Point d'attention.** `pgbr-reader` détient la phrase de chiffrement, puisque le
déchiffrement se fait côté dépôt. Un attaquant maître de server01 peut donc **lire** les
sauvegardes. Ce sont les données de server01 lui-même : risque jugé acceptable. Il ne peut
pas les altérer.

## 3. Dépôt pgBackRest : disque local d'OdooGuard (et pas NFS)

**Choix : un disque virtuel dédié (VMDK) monté sur `/srv/odooguard`, en ext4.**

| Critère | Disque local (VMDK) | NFS (NAS 192.168.8.30) | CIFS |
|---|---|---|---|
| Sémantique POSIX (verrous, fsync, renommage atomique) | garantie | correcte en NFSv4 bien configuré (`hard`) | **non garantie** → exclu |
| Dépendance au NAS déjà sujet aux erreurs d'E/S | aucune | chaque WAL en dépend : un NAS qui flanche bloque l'archivage et fait monter le RPO | — |
| Débit de restauration (≈ 100 Go) | celui du datastore | limité par le réseau et le NAS | — |
| Indépendance vis-à-vis de server01 | même hôte ESXi (voir mitigation) | matériel distinct | — |

Le RPO dépend du chemin d'écriture des WAL. On ne le fait donc pas dépendre d'un
NAS qui a déjà renvoyé des erreurs d'E/S. Le risque du disque local, perdre le même
hôte ESXi ou le même datastore que server01, est couvert par :

* une **copie horaire vérifiée** des deux dépôts sur **deux NAS** (8.30 et 3.19). Chaque
  copie est elle-même un dépôt utilisable sans OdooGuard ;
* Veeam (sauvegarde de la VM OdooGuard) et la copie hors ligne G:\ (phase 3 bis) ;
* la recommandation de placer le VMDK des dépôts sur **un autre datastore** que server01
  si l'ESXi en possède un. [À VÉRIFIER]

Si le NAS principal propose un export NFSv4 fiable, on peut l'ajouter plus tard
comme `repo2` pgBackRest : multi-dépôt natif, toujours alimenté depuis OdooGuard.
Ce n'est pas nécessaire en phase 1.

**Dimensionnement indicatif du VMDK** (à ajuster après la 1re semaine avec `odooguard list`) :

| Poste | Estimation |
|---|---|
| pgBackRest : 4 complètes (zstd) + différentielles + WAL de 4 semaines | 150 à 250 Go |
| restic : filestore + modules + config (≈ 60 Go dédupliqués) + historique | 80 à 120 Go |
| Miroir rsync (filestore, modules, config) | ≈ 60 Go |
| Zone de travail des restaurations (une base) | ≈ 60 Go |
| **Total recommandé** | **1 To** (provisionnement dynamique accepté) |

Exclure ce VMDK de Veeam (mode *independent-persistent*), sauf choix contraire : les
NAS en ont déjà deux copies.

## 4. Planification (systemd timers)

On utilise les **timers systemd** plutôt qu'APScheduler : ils fonctionnent même si le
tableau de bord web (phase 2) est arrêté, rattrapent les exécutions manquées
(`Persistent=true`) et journalisent dans journald. Le tableau de bord n'est donc pas un
point de défaillance.

| Unité | Planification | Rôle |
|---|---|---|
| `odooguard-walreceiver.service` | permanent | réception des WAL |
| `odooguard-walingest.service` | permanent | versement des WAL dans le dépôt |
| `odooguard-backup-db-full.timer` | dimanche 01:00 | complète |
| `odooguard-backup-db-diff.timer` | lundi–samedi 01:00 | différentielle |
| `odooguard-backup-db-incr.timer` | lundi–samedi 08:05 → 18:05, toutes les heures | incrémentale |
| `odooguard-backup-files.timer` | toutes les 30 min (:20, :50) | filestore, modules, config |
| `odooguard-copy.timer` | toutes les heures (:40) | copie vérifiée vers les NAS |
| `odooguard-verify-quick.timer` | tous les jours 06:10 | `pgbackrest check` + `restic check` (2 %) + NAS |
| `odooguard-verify-full.timer` | samedi 13:00 | relecture intégrale (`pgbackrest verify`, restic 100 %) |
| `odooguard-maintenance.timer` | dimanche 05:00 | rétention restic (`forget --prune`) |
| `odooguard-status.timer` | toutes les 5 min | état, mesure du RPO, alertes |
| `odooguard-restore-test.timer` | samedi 06:30 | test de restauration complet sur la VM de test, rapport (phase 3) |
| `odooguard-web.service` / `odooguard-jobs.service` | permanents | tableau de bord ; exécution des demandes de l'assistant (phases 2 et 3) |

Toute unité en échec déclenche `odooguard-notify@<unité>` (`OnFailure=`) : l'alerte part
immédiatement. Le contrôle toutes les 5 min détecte en plus un NAS absent, un montage
figé (test d'écriture réel avec relecture), un retard des WAL ou un disque plein. Le délai
d'alerte est donc toujours inférieur à 15 min.

## 5. Ce qui garantit qu'une sauvegarde partielle n'est jamais « valide »

* pgBackRest n'inscrit une sauvegarde dans `backup.info` qu'une fois terminée,
  WAL de cohérence compris (`archive-check=y`). Une sauvegarde interrompue reste
  invisible et est nettoyée par la suivante.
* restic n'écrit l'instantané qu'en fin de sauvegarde. Si des fichiers sont illisibles
  (code 3), OdooGuard **supprime** l'instantané incomplet et signale l'échec.
* Si rsync échoue, restic n'est pas lancé : aucun instantané.
* La copie vers un NAS n'est déclarée réussie qu'après relecture : `pgbackrest info` sur
  la copie doit montrer la même dernière sauvegarde, et l'instantané restic doit être présent.
* `odooguard status` lit l'état dans pgBackRest et restic, pas seulement dans l'historique.

## 6. Versions : Ubuntu (OdooGuard) et Debian (server01)

pgBackRest exige **la même version** aux deux bouts du protocole SSH, et `pg_receivewal`,
`pg_dump` et `pg_ctl` doivent avoir la même version majeure que le serveur. Les deux machines
utilisent donc le **dépôt officiel apt.postgresql.org**, qui fournit les mêmes versions pour
Debian 12 et Ubuntu 24.04. Après chaque mise à jour, comparer `pgbackrest version` sur
les deux machines. Si les versions diffèrent, les sauvegardes échouent et l'alerte part.
Il faut mettre à jour les deux machines ensemble.

## 7. Objectifs de reprise (estimations à confirmer en recette)

| Scénario | Perte de données (RPO) | Durée (RTO) |
|---|---|---|
| Erreur utilisateur → base restaurée à un instant précis (copie à côté) | 0 (choix de l'instant) | 30 à 60 min pour 16 Go de dump |
| server01 perdu, OdooGuard intact | quelques secondes (segment `.partial`) | 1 à 2 h (Debian neuf + restauration) |
| server01 et OdooGuard perdus, NAS intacts | ≤ 1 h (copie horaire) | 2 à 4 h, procédure manuelle |
