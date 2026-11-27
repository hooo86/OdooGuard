"""Gestion des NAS depuis le tableau de bord (ajout, modification, test, retrait).

Chaîne de responsabilité (aucune élévation de droits dans le service web) :

  tableau de bord (cloisonné)  -> enregistre la demande (table jobs) ; le mot de
                                  passe éventuel va dans un fichier 600 à usage
                                  unique (<state_dir>/pending-secrets/<jeton>),
                                  jamais dans la base ;
  odooguard-jobs (odooguard)   -> re-valide, lit puis efface le mot de passe,
                                  appelle le programme root ci-dessous, met à jour
                                  /etc/odooguard/odooguard.conf, teste l'écriture ;
  odooguard-nas-helper (root,  -> écrit les identifiants (600), l'unité systemd
  via sudo, liste blanche)        de montage, monte /mnt/odooguard/<nom>.

La section ``[destination:<nom>]`` de la configuration est remplacée en place :
les commentaires et les autres sections du fichier sont conservés.
"""

from __future__ import annotations

import json
import os
import re

from odooguard.config import Config, Destination
from odooguard.util import OdooGuardError, free_gb, io_probe, run

HELPER = "/usr/local/sbin/odooguard-nas-helper"
MOUNT_BASE = "/mnt/odooguard"
NAME_RE = re.compile(r"^[a-z][a-z0-9]{0,15}$")
SHARE_RE = re.compile(r"^//[A-Za-z0-9.-]{1,253}/[A-Za-z0-9_.$-]{1,80}$")
USER_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
DOMAIN_RE = re.compile(r"^[A-Za-z0-9_.-]{0,64}$")
SUBDIR_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
VERSIONS = ("3.1.1", "3.0", "2.1", "2.0")


# ------------------------------------------------------------------ validation
def validate(p: dict, *, require_password: bool) -> dict:
    name = str(p.get("name", "")).strip().lower()
    if not NAME_RE.match(name):
        raise OdooGuardError("nom : lettres minuscules et chiffres, 16 caractères au plus (ex. nas1)", "usage")
    share = str(p.get("share", "")).strip().replace("\\", "/")
    if not SHARE_RE.match(share):
        raise OdooGuardError("partage : forme //serveur/partage (ex. //192.168.8.30/odoo), sans espace", "usage")
    user = str(p.get("username", "")).strip()
    if not USER_RE.match(user):
        raise OdooGuardError("compte : lettres, chiffres, . _ @ - (64 caractères au plus)", "usage")
    domain = str(p.get("domain", "") or "").strip()
    if not DOMAIN_RE.match(domain):
        raise OdooGuardError("domaine invalide", "usage")
    vers = str(p.get("smb_version", "3.0"))
    if vers not in VERSIONS:
        raise OdooGuardError(f"version SMB : {', '.join(VERSIONS)}", "usage")
    subdir = str(p.get("subdir", "odooguard") or "odooguard").strip()
    if not SUBDIR_RE.match(subdir) or subdir in (".", ".."):
        raise OdooGuardError("dossier : un seul nom de dossier (ex. odooguard)", "usage")
    try:
        min_free = float(p.get("min_free_gb", 100))
    except (TypeError, ValueError):
        raise OdooGuardError("espace minimal : nombre de Gio", "usage") from None
    if not 0 <= min_free <= 100000:
        raise OdooGuardError("espace minimal hors limites", "usage")
    if require_password and not p.get("has_password"):
        raise OdooGuardError("mot de passe obligatoire pour un nouveau NAS", "usage")
    return {"name": name, "share": share, "username": user, "domain": domain, "smb_version": vers,
            "subdir": subdir, "min_free_gb": min_free, "enabled": bool(p.get("enabled", True))}


# ------------------------------------------------------------------ secrets et configuration
from odooguard import settings as _settings  # noqa: E402


def stash_password(cfg: Config, password: str) -> str:
    """Appelé par le service web : dépose le mot de passe, renvoie un jeton."""
    if len(password) > 256:
        raise OdooGuardError("mot de passe invalide (256 caractères au plus)", "usage")
    return _settings.stash_secret(cfg, password)


def take_password(cfg: Config, token: str | None) -> str:
    """Appelé par le service de travaux : lit et efface le mot de passe déposé."""
    return _settings.take_secret(cfg, token)


def write_destination(cfg_path: str, name: str, values: dict | None) -> None:
    """Remplace (ou ajoute, ou retire si values=None) la section [destination:<nom>]."""
    _settings.write_section(cfg_path, f"destination:{name}", values,
                            comment="Géré depuis le tableau de bord (page NAS)")


def config_values(v: dict) -> dict:
    mp = f"{MOUNT_BASE}/{v['name']}"
    return {"share": v["share"], "username": v["username"], "domain": v["domain"],
            "smb_version": v["smb_version"], "subdir": v["subdir"], "mountpoint": mp,
            "path": f"{mp}/{v['subdir']}", "min_free_gb": f"{v['min_free_gb']:g}",
            "enabled": "yes" if v["enabled"] else "no"}


# ------------------------------------------------------------------ programme root
def helper(*args: str, password: str = "", timeout: float = 120) -> dict:
    res = run(["sudo", "-n", HELPER, *args], input=password + "\n", check=False, timeout=timeout)
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith("{")]
    data = json.loads(lines[-1]) if lines else {}
    if res.returncode != 0 or not data.get("ok"):
        msg = data.get("error") or (res.stderr or res.stdout).strip()[-500:]
        raise OdooGuardError(f"NAS : {msg}", "mount")
    return data


def ensure_mounted(dest: Destination) -> None:
    """Avant une copie : remonte un NAS géré qui ne l'est plus (coupure réseau, redémarrage)."""
    if dest.share and dest.mountpoint and not os.path.ismount(dest.mountpoint):
        helper("ensure", dest.name)


# ------------------------------------------------------------------ travaux
def job_apply(cfg: Config, params: dict) -> dict:
    is_new = params.get("name") not in cfg.destinations or not cfg.destinations[params["name"]].share
    v = validate(params, require_password=is_new)
    password = take_password(cfg, params.get("secret_token"))
    if is_new and not password:
        raise OdooGuardError("mot de passe obligatoire pour un nouveau NAS", "usage")
    out = helper("apply", v["name"], v["share"], v["smb_version"], v["username"], v["domain"] or "-",
                 password=password)
    values = config_values(v)
    os.makedirs(values["path"], exist_ok=True)
    probe = io_probe(values["path"])
    free = free_gb(values["path"])
    write_destination(cfg.path, v["name"], values)
    return {"name": v["name"], "share": v["share"], "path": values["path"], "mounted": out.get("ok"),
            "fstab_lines_disabled": out.get("fstab_lines_disabled", 0),
            "probe_seconds": round(probe, 3), "free_gb": round(free, 1),
            "message": f"NAS {v['name']} monté sur {values['mountpoint']}, {free:.0f} Gio libres"}


MIB = 1024 * 1024


def benchmark(dest: Destination, size_mb: int = 128, small_files: int = 100) -> dict:
    """Mesure du NAS dans un dossier temporaire (supprimé ensuite).

    * débit d'écriture puis de relecture d'un fichier de ``size_mb`` Mio ;
    * temps par petit fichier, comme rsync : écriture d'un fichier temporaire, fermeture,
      renommage ; c'est ce qui limite la copie de milliers de petits fichiers sur SMB ;
    * conservation des dates : date imposée au fichier temporaire, puis renommage, puis
      relecture après expiration du cache d'attributs du client (2 s).
    """
    import secrets
    import shutil
    import time
    d = os.path.join(dest.path, f".odooguard-mesure-{secrets.token_hex(4)}")
    os.makedirs(d)
    out = {"measured_at": time.time(), "size_mb": size_mb, "small_files": small_files}
    try:
        buf = os.urandom(MIB)
        big = os.path.join(d, "bloc.bin")
        t = time.monotonic()
        with open(big, "wb") as fh:
            for _ in range(size_mb):
                fh.write(buf)
            fh.flush()
            os.fsync(fh.fileno())
        out["write_mbps"] = round(size_mb / max(time.monotonic() - t, 1e-6), 1)
        fd = os.open(big, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)  # relire le NAS, pas le cache local
            t = time.monotonic()
            while os.read(fd, MIB):
                pass
            out["read_mbps"] = round(size_mb / max(time.monotonic() - t, 1e-6), 1)
        finally:
            os.close(fd)
        payload = os.urandom(4096)
        t = time.monotonic()
        for i in range(small_files):
            tmp = os.path.join(d, f".f{i}.tmp")
            with open(tmp, "wb") as fh:
                fh.write(payload)
            os.rename(tmp, os.path.join(d, f"f{i}"))
        el = max(time.monotonic() - t, 1e-6)
        out["files_per_s"] = round(small_files / el, 1)
        out["ms_per_file"] = round(1000 * el / small_files)
        stamp = 1577836800  # 2020-01-01 00:00:00 UTC
        tmp, final = os.path.join(d, ".date.tmp"), os.path.join(d, "date")
        with open(tmp, "wb") as fh:
            fh.write(payload)
        os.utime(tmp, (stamp, stamp))
        os.rename(tmp, final)
        time.sleep(2.5)
        out["dates_kept"] = abs(os.stat(final).st_mtime - stamp) <= 2
    finally:
        shutil.rmtree(d, ignore_errors=True)
    notes = []
    if not out.get("dates_kept"):
        notes.append("dates non conservées : la copie compare les fichiers sur leur taille (déjà géré)")
    if out.get("ms_per_file", 0) >= 100:
        notes.append(f"{out['ms_per_file']} ms par fichier : lent pour de nombreux petits fichiers "
                     "(regroupement pgBackRest repo1-bundle recommandé)")
    if out.get("write_mbps", 999) < 20:
        notes.append(f"écriture lente ({out['write_mbps']} Mo/s) : vérifier le réseau et les disques du NAS")
    out["notes"] = notes
    return out


def job_test(cfg: Config, params: dict, history=None) -> dict:
    from odooguard import dest as dest_mod
    name = str(params.get("name", ""))
    dest = cfg.destinations.get(name)
    if dest is None:
        raise OdooGuardError(f"NAS inconnu : {name}", "usage")
    ensure_mounted(dest)
    info = dest_mod.preflight(dest)
    free = free_gb(dest.path)
    bench = benchmark(dest)
    if history is not None:
        history.kv_put(f"nas_bench:{name}", bench)
    msg = (f"NAS {name} accessible, {free:.0f} Gio libres ; écriture {bench['write_mbps']} Mo/s, "
           f"relecture {bench['read_mbps']} Mo/s, {bench['ms_per_file']} ms par petit fichier, dates "
           + ("conservées" if bench["dates_kept"] else "NON conservées"))
    if bench["notes"]:
        msg += " ; " + " ; ".join(bench["notes"])
    return {"name": name, "free_gb": round(free, 1), **info, "benchmark": bench, "message": msg}


def job_remove(cfg: Config, params: dict) -> dict:
    name = str(params.get("name", ""))
    if name not in cfg.destinations:
        raise OdooGuardError(f"NAS inconnu : {name}", "usage")
    if params.get("confirm") != name:
        raise OdooGuardError(f"confirmation : retaper exactement « {name} »", "usage")
    if cfg.destinations[name].share:
        helper("remove", name)
    write_destination(cfg.path, name, None)
    return {"name": name, "message": f"NAS {name} retiré (les sauvegardes déjà présentes sur le NAS sont conservées)"}


def refresh_status(cfg: Config, history) -> None:
    """Met à jour status.json pour que le tableau de bord reflète aussitôt le changement."""
    from odooguard import config as config_mod
    from odooguard import status as status_mod
    fresh = config_mod.load(cfg.path)
    st = status_mod.compute(fresh, history, query_source=False)
    status_mod.record(fresh, history, st)
