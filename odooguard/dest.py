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

import hashlib
import json
import logging
import os
import time

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


MANIFEST = "MANIFEST.json"
MANIFEST_FILES = "MANIFEST.files.tsv"


def _atomic_write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_manifest(cfg: Config, dest: Destination, pg_label: str, restic_snapshots: int) -> dict:
    """Écrit la liste des fichiers (chemin, taille) d'une copie NAS VÉRIFIÉE.

    Utilisé par la copie hors ligne vers G:\\ (deploy/windows/Copy-OdooGuardOffline.ps1) :
    elle copie l'arborescence puis contrôle que CHAQUE fichier listé est présent
    sur G:\\ avec la même taille. Une copie interrompue, ou faite pendant
    qu'OdooGuard modifiait le NAS, est ainsi détectée au lieu d'être prise pour
    une copie valide. Écrit seulement après la vérification de la copie NAS.

    Deux fichiers (Windows PowerShell 5.1 ne lit pas les JSON de plus de 2 Mo) :
    * ``MANIFEST.files.tsv`` : une ligne « chemin<TAB>taille » par fichier ;
    * ``MANIFEST.json`` : en-tête (date, dernière sauvegarde, totaux, SHA-256 du TSV),
      écrit en dernier : il ne désigne jamais un TSV incomplet.
    """
    rows = []
    total = 0
    for sub in ("pgbackrest", "restic"):
        root = os.path.join(dest.path, sub)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "locks"]  # verrous restic : transitoires
            for f in filenames:
                if f.endswith(".tmp"):
                    continue
                full = os.path.join(dirpath, f)
                size = os.path.getsize(full)
                rows.append((os.path.relpath(full, dest.path).replace(os.sep, "/"), size))
                total += size
    rows.sort()
    tsv = "".join(f"{p}\t{n}\n" for p, n in rows).encode("utf-8")
    _atomic_write(os.path.join(dest.path, MANIFEST_FILES), tsv)
    header = {"format": 1, "site": cfg.site, "destination": dest.name,
              "generated_at": int(time.time()),
              "generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "pgbackrest_last_backup": pg_label, "restic_snapshots": restic_snapshots,
              "file_count": len(rows), "total_bytes": total,
              "files_sha256": hashlib.sha256(tsv).hexdigest()}
    _atomic_write(os.path.join(dest.path, MANIFEST),
                  json.dumps(header, indent=2, ensure_ascii=False).encode("utf-8"))
    return {"file_count": len(rows), "total_bytes": total}
