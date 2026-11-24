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
import secrets
import time

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
SECRET_TTL = 3600


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


# ------------------------------------------------------------------ mot de passe à usage unique
def _secrets_dir(cfg: Config) -> str:
    d = os.path.join(cfg.state_dir, "pending-secrets")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def stash_password(cfg: Config, password: str) -> str:
    """Appelé par le service web : dépose le mot de passe, renvoie un jeton."""
    if len(password) > 256 or "\n" in password or "\x00" in password:
        raise OdooGuardError("mot de passe invalide (une ligne, 256 caractères au plus)", "usage")
    token = secrets.token_hex(16)
    path = os.path.join(_secrets_dir(cfg), token)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(password)
    return token


def take_password(cfg: Config, token: str | None) -> str:
    """Appelé par le service de travaux : lit et efface le mot de passe déposé."""
    purge_stale_secrets(cfg)
    if not token:
        return ""
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise OdooGuardError("jeton de mot de passe invalide", "usage")
    path = os.path.join(_secrets_dir(cfg), token)
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        raise OdooGuardError("mot de passe expiré : ressaisir le formulaire", "usage") from None
    finally:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def purge_stale_secrets(cfg: Config) -> None:
    d = _secrets_dir(cfg)
    for f in os.listdir(d):
        path = os.path.join(d, f)
        try:
            if time.time() - os.stat(path).st_mtime > SECRET_TTL:
                os.remove(path)
        except FileNotFoundError:
            pass


# ------------------------------------------------------------------ configuration
SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def write_destination(cfg_path: str, name: str, values: dict | None) -> None:
    """Remplace (ou ajoute, ou retire si values=None) la section [destination:<nom>]."""
    with open(cfg_path, encoding="utf-8") as fh:
        lines = fh.read().splitlines(keepends=True)
    header = f"destination:{name}"
    start = end = None
    for i, line in enumerate(lines):
        m = SECTION_RE.match(line)
        if m and m.group(1).strip() == header:
            start = i
        elif m and start is not None and end is None:
            end = i
    if start is not None and end is None:
        end = len(lines)
    block = []
    if values is not None:
        block = [f"[{header}]\n", "# Géré depuis le tableau de bord (page NAS)\n"]
        block += [f"{k} = {v}\n" for k, v in values.items()]
        block.append("\n")
    if start is None:
        new = lines + (["\n"] if lines and lines[-1].strip() else []) + block
    else:
        new = lines[:start] + block + lines[end:]
    backup = cfg_path + ".avant-nas"
    st = os.stat(cfg_path)
    with open(backup, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    os.chmod(backup, 0o600)
    tmp = cfg_path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.writelines(new)
    try:
        os.chown(tmp, st.st_uid, st.st_gid)
    except PermissionError:
        pass
    os.replace(tmp, cfg_path)


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


def job_test(cfg: Config, params: dict) -> dict:
    from odooguard import dest as dest_mod
    name = str(params.get("name", ""))
    dest = cfg.destinations.get(name)
    if dest is None:
        raise OdooGuardError(f"NAS inconnu : {name}", "usage")
    ensure_mounted(dest)
    info = dest_mod.preflight(dest)
    free = free_gb(dest.path)
    return {"name": name, "free_gb": round(free, 1), **info,
            "message": f"NAS {name} accessible : écriture et relecture OK, {free:.0f} Gio libres"}


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
