"""Copies de second niveau vers les NAS (192.168.8.30, 192.168.3.19...).

Les dépôts primaires (pgBackRest, restic) sont sur le disque local
d'OdooGuard. Chaque heure, ils sont recopiés (incrémentalement) sur chaque destination :

* dépôt pgBackRest : ``rsync`` du répertoire (un dépôt pgBackRest est un simple
  arbre de fichiers immuables + fichiers ``*.info`` ; la copie, faite sous
  verrou après la sauvegarde, est elle-même un dépôt valide, restaurable avec
  ``--repo1-path`` sans OdooGuard) ;
* dépôt restic : ``restic copy`` (dédupliqué, chiffré).

Avant toute copie : point de montage actif, test d'écriture réel avec relecture
(erreurs d'E/S déjà constatées sur le NAS principal), espace libre. Après :
contrôle que la destination liste bien la dernière sauvegarde.
"""

from __future__ import annotations

import logging
import os

from odooguard.config import Config, Destination
from odooguard.files import Restic
from odooguard.pg import PgBackRest
from odooguard.util import OdooGuardError, check_free_space, io_probe, run

log = logging.getLogger("odooguard")


def preflight(dest: Destination) -> dict:
    if dest.mountpoint and not os.path.ismount(dest.mountpoint):
        raise OdooGuardError(
            f"destination {dest.name} : {dest.mountpoint} n'est pas monté (NAS absent ?)", "mount")
    if not os.path.isdir(dest.path):
        raise OdooGuardError(f"destination {dest.name} : répertoire {dest.path} absent", "mount")
    check_free_space(dest.path, dest.min_free_gb, f"destination {dest.name}")
    probe = io_probe(dest.path)
    return {"probe_seconds": round(probe, 3)}


def copy_pgbackrest(cfg: Config, dest: Destination) -> dict:
    target = os.path.join(dest.path, "pgbackrest")
    os.makedirs(target, exist_ok=True)
    # --delete-after : les sauvegardes expirées localement sont retirées de la copie
    # seulement une fois tout le reste transféré. Pas d'ACL/propriétaire (CIFS).
    res = run(["rsync", "-rt", "--delete-after", "--stats", "--timeout=900",
               cfg.repo_path.rstrip("/") + "/", target + "/"], timeout=6 * 3600)
    # Contrôle : la copie doit être lisible par pgBackRest et contenir la même
    # dernière sauvegarde que le dépôt local.
    local = PgBackRest(cfg).info().last()
    remote = PgBackRest(cfg).info(repo_path=target).last()
    if not local or not remote or local.label != remote.label:
        raise OdooGuardError(
            f"destination {dest.name} : la copie pgBackRest ne contient pas la dernière "
            f"sauvegarde ({local.label if local else '-'})", "io")
    return {"label": remote.label, "rsync": res.stdout[-600:]}


def restic_for(cfg: Config, dest: Destination) -> Restic:
    return Restic(cfg, repo=os.path.join(dest.path, "restic"),
                  password_file=dest.restic_password_file or cfg.restic_password_file)


def copy_restic(cfg: Config, dest: Destination) -> dict:
    local = Restic(cfg)
    remote = restic_for(cfg, dest)
    if not os.path.exists(os.path.join(remote.repo, "config")):
        remote.init(copy_from=local)
    local.copy_to(remote)
    last_local = (local.snapshots() or [{}])[-1].get("id")
    remote_ids = {s["id"] for s in remote.snapshots()}
    if last_local and last_local not in remote_ids:
        # restic copy conserve les identifiants d'origine dans « original »
        originals = {s.get("original") for s in remote.snapshots()}
        if last_local not in originals:
            raise OdooGuardError(f"destination {dest.name} : dernier instantané restic absent", "io")
    return {"snapshots": len(remote_ids)}
