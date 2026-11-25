"""Réglages modifiables depuis le tableau de bord : notifications, comptes.

Même chaîne que pour les NAS : le service web (cloisonné, /etc en lecture
seule) enregistre une demande ; le service odooguard-jobs la re-valide et
l'applique. Les secrets (mot de passe SMTP, jeton Telegram, empreinte de mot
de passe d'un compte) ne passent jamais par la base : ils sont déposés dans un
fichier 600 à usage unique, lu puis effacé par le service de travaux.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from odooguard.config import Config
from odooguard.util import OdooGuardError

SECRET_TTL = 3600
SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


# ------------------------------------------------------------------ secrets à usage unique
def _secrets_dir(cfg: Config) -> str:
    d = os.path.join(cfg.state_dir, "pending-secrets")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def stash_secret(cfg: Config, value: str) -> str:
    if len(value) > 512 or "\n" in value or "\x00" in value:
        raise OdooGuardError("valeur secrète invalide (une ligne, 512 caractères au plus)", "usage")
    token = secrets.token_hex(16)
    fd = os.open(os.path.join(_secrets_dir(cfg), token), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value)
    return token


def take_secret(cfg: Config, token: str | None) -> str:
    purge_stale_secrets(cfg)
    if not token:
        return ""
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise OdooGuardError("jeton de secret invalide", "usage")
    path = os.path.join(_secrets_dir(cfg), token)
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        raise OdooGuardError("secret expiré : ressaisir le formulaire", "usage") from None
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


def write_secret_file(path: str, value: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value + "\n")
    os.replace(tmp, path)


# ------------------------------------------------------------------ fichier de configuration
def write_section(cfg_path: str, header: str, values: dict | None, comment: str = "") -> None:
    """Remplace (ou ajoute, ou retire si values=None) la section [header].

    Les autres sections et les commentaires hors de cette section sont conservés.
    Copie de sécurité : <fichier>.avant-tableau-de-bord.
    """
    with open(cfg_path, encoding="utf-8") as fh:
        lines = fh.read().splitlines(keepends=True)
    start = end = None
    for i, line in enumerate(lines):
        m = SECTION_RE.match(line)
        if m and m.group(1).strip() == header:
            start = i
        elif m and start is not None and end is None:
            end = i
    if start is not None and end is None:
        end = len(lines)
    block: list[str] = []
    if values is not None:
        block = [f"[{header}]\n", f"# {comment or 'Modifié depuis le tableau de bord'}\n"]
        for k, v in values.items():
            if "\n" in str(v):
                raise OdooGuardError(f"valeur invalide pour {k}", "usage")
            block.append(f"{k} = {v}\n")
        block.append("\n")
    if start is None:
        new = lines + (["\n"] if lines and lines[-1].strip() else []) + block
    else:
        new = lines[:start] + block + lines[end:]
    _commit(cfg_path, lines, new)


def set_option(cfg_path: str, header: str, key: str, value: str) -> None:
    """Change UNE clé d'une section existante, sans toucher au reste de la section."""
    if "\n" in str(value):
        raise OdooGuardError(f"valeur invalide pour {key}", "usage")
    with open(cfg_path, encoding="utf-8") as fh:
        lines = fh.read().splitlines(keepends=True)
    start = end = None
    for i, line in enumerate(lines):
        m = SECTION_RE.match(line)
        if m and m.group(1).strip() == header:
            start = i
        elif m and start is not None and end is None:
            end = i
    if start is None:
        raise OdooGuardError(f"section [{header}] absente de la configuration", "config")
    end = len(lines) if end is None else end
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*[=:]")
    new_line = f"{key} = {value}\n"
    for i in range(start + 1, end):
        if key_re.match(lines[i]):
            j = i + 1  # lignes de continuation (indentées) de l'ancienne valeur
            while j < end and lines[j][:1] in (" ", "\t") and lines[j].strip():
                j += 1
            new = lines[:i] + [new_line] + lines[j:]
            break
    else:
        new = lines[:start + 1] + [new_line] + lines[start + 1:]
    _commit(cfg_path, lines, new)


def _commit(cfg_path: str, lines: list[str], new: list[str]) -> None:
    from odooguard import config as config_mod
    try:
        config_mod.load(cfg_path, strict_perms=False)
        was_valid = True
    except config_mod.ConfigError:
        was_valid = False
    st = os.stat(cfg_path)
    backup = cfg_path + ".avant-tableau-de-bord"
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
    # Vérification : un fichier valide doit le rester, sinon retour à l'ancienne version
    if not was_valid:
        return
    try:
        config_mod.load(cfg_path, strict_perms=False)
    except config_mod.ConfigError as exc:
        os.replace(backup, cfg_path)
        raise OdooGuardError(f"configuration refusée, ancienne version restaurée : {exc}", "config") from None


# ------------------------------------------------------------------ notifications
EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
CHAT_RE = re.compile(r"^-?\d{1,20}$|^@[A-Za-z0-9_]{5,32}$")
PROXY_RE = re.compile(r"^https?://[A-Za-z0-9.:@_-]+/?$")


def _split(v) -> list[str]:
    return [x.strip() for x in re.split(r"[,;\s]+", str(v or "")) if x.strip()]


def validate_notifications(p: dict) -> dict:
    channels = ["journal"] + [c for c in ("email", "telegram") if p.get(f"use_{c}")]
    min_level = p.get("min_level", "warning")
    if min_level not in ("warning", "critical"):
        raise OdooGuardError("niveau minimal : warning ou critical", "usage")
    try:
        repeat = float(p.get("repeat_hours", 4))
    except (TypeError, ValueError):
        raise OdooGuardError("rappel : nombre d'heures", "usage") from None
    if not 0.25 <= repeat <= 168:
        raise OdooGuardError("rappel : entre 0,25 et 168 heures", "usage")
    host = str(p.get("smtp_host", "")).strip()
    if host and not HOST_RE.match(host):
        raise OdooGuardError("serveur SMTP : nom ou adresse IP", "usage")
    try:
        port = int(p.get("smtp_port") or 587)
    except ValueError:
        raise OdooGuardError("port SMTP invalide", "usage") from None
    if not 1 <= port <= 65535:
        raise OdooGuardError("port SMTP invalide", "usage")
    security = p.get("smtp_security", "starttls")
    if security not in ("starttls", "ssl", "none"):
        raise OdooGuardError("sécurité SMTP : starttls, ssl ou none", "usage")
    user = str(p.get("smtp_user", "")).strip()
    if user and not re.fullmatch(r"[^\s,;]{1,128}", user):
        raise OdooGuardError("compte SMTP invalide", "usage")
    mail_from = str(p.get("mail_from", "")).strip()
    mail_to = _split(p.get("mail_to"))
    for addr in ([mail_from] if mail_from else []) + mail_to:
        if not EMAIL_RE.match(addr):
            raise OdooGuardError(f"adresse e-mail invalide : {addr}", "usage")
    chats = _split(p.get("telegram_chat_ids"))
    for c in chats:
        if not CHAT_RE.match(c):
            raise OdooGuardError(f"identifiant Telegram invalide : {c} (nombre, ex. -1001234567890)", "usage")
    proxy = str(p.get("https_proxy", "")).strip()
    if proxy and not PROXY_RE.match(proxy):
        raise OdooGuardError("proxy : http://hôte:port", "usage")
    if "email" in channels and not (host and mail_from and mail_to):
        raise OdooGuardError("e-mail activé : serveur, expéditeur et destinataires sont obligatoires", "usage")
    if "telegram" in channels and not chats:
        raise OdooGuardError("Telegram activé : au moins un identifiant de conversation", "usage")
    routes = {}
    if p.get("routes_present"):
        for cat in p.get("route_categories") or []:
            routes[cat] = [c for c in ("email", "telegram") if p.get(f"route_{cat}_{c}")]
    return {"routes": routes, "channels": channels, "min_level": min_level, "repeat_hours": repeat,
            "send_resolved": bool(p.get("send_resolved")), "smtp_host": host, "smtp_port": port,
            "smtp_security": security, "smtp_user": user, "mail_from": mail_from, "mail_to": mail_to,
            "telegram_chat_ids": chats, "https_proxy": proxy}


def job_notify_save(cfg: Config, params: dict) -> dict:
    from odooguard.config import notify_categories
    v = validate_notifications({**params, "route_categories": list(notify_categories(cfg))})
    a = cfg.alerts
    smtp_pw_file = a.smtp_password_file or os.path.join(os.path.dirname(cfg.path), "smtp.pass")
    tg_file = a.telegram_token_file or os.path.join(os.path.dirname(cfg.path), "telegram.token")
    smtp_pw = take_secret(cfg, params.get("smtp_password_token"))
    token = take_secret(cfg, params.get("telegram_token_token"))
    if token and not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{30,64}", token):
        raise OdooGuardError("jeton Telegram invalide (forme 123456789:ABC...)", "usage")
    if smtp_pw:
        write_secret_file(smtp_pw_file, smtp_pw)
    if token:
        write_secret_file(tg_file, token)
    if "telegram" in v["channels"] and not token and not _nonempty(tg_file):
        raise OdooGuardError("Telegram activé : jeton du bot obligatoire", "usage")
    alerts = {"channels": ", ".join(v["channels"]), "min_level": v["min_level"],
              "repeat_hours": f"{v['repeat_hours']:g}", "send_resolved": "yes" if v["send_resolved"] else "no"}
    routes = {**a.routes, **v["routes"]}  # formulaire sans le tableau : réglage par type conservé
    for cat, chans in routes.items():
        alerts[f"route_{cat}"] = ", ".join(chans) if chans else "aucun"
    write_section(cfg.path, "alerts", alerts)
    write_section(cfg.path, "smtp", {
        "host": v["smtp_host"], "port": v["smtp_port"], "security": v["smtp_security"],
        "user": v["smtp_user"], "password_file": smtp_pw_file, "from": v["mail_from"],
        "to": ", ".join(v["mail_to"])})
    write_section(cfg.path, "telegram", {
        "token_file": tg_file, "chat_ids": ", ".join(v["telegram_chat_ids"]),
        "api": a.telegram_api, "https_proxy": v["https_proxy"]})
    return {"channels": v["channels"], "smtp_password_changed": bool(smtp_pw),
            "telegram_token_changed": bool(token),
            "message": "réglages des notifications enregistrés (canaux : " + ", ".join(v["channels"]) + ")"}


def _nonempty(path: str) -> bool:
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


TEST_CHANNELS = {"": "tous les canaux enregistrés", "email": "e-mail", "telegram": "Telegram"}


def job_notify_test(cfg: Config, history, channel: str = "") -> dict:
    from odooguard import notify
    if channel not in TEST_CHANNELS:
        raise OdooGuardError("canal de test : email ou telegram", "usage")
    if channel == "telegram" and not (cfg.alerts.telegram_token_file and cfg.alerts.telegram_chat_ids):
        raise OdooGuardError("Telegram non configuré : enregistrer d'abord le jeton du bot et "
                             "l'identifiant de conversation", "usage")
    if channel == "email" and not (cfg.alerts.smtp_host and cfg.alerts.mail_to):
        raise OdooGuardError("e-mail non configuré : enregistrer d'abord le serveur SMTP et les "
                             "destinataires", "usage")
    res = notify.dispatch(cfg, "warning", f"message de test ({TEST_CHANNELS[channel]})",
                          "Si vous lisez ceci, ce canal d'alerte OdooGuard fonctionne.",
                          key="test", history=history, force_all=True, only=channel or None)
    failed = {k: v for k, v in res.items() if v != "ok"}
    if failed:
        raise OdooGuardError("échec d'envoi : " + " ; ".join(f"{k} : {v}" for k, v in failed.items()), "command")
    return {"channels": res, "message": "message de test envoyé : " + ", ".join(res)}


def job_telegram_discover(cfg: Config, params: dict) -> dict:
    """Liste les conversations où le bot a reçu un message (pour trouver chat_id)."""
    token = take_secret(cfg, params.get("token_token"))
    if not token:
        try:
            with open(cfg.alerts.telegram_token_file, encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError:
            token = ""
    if not token:
        raise OdooGuardError("jeton du bot absent : le saisir dans le formulaire", "usage")
    handlers = []
    if cfg.alerts.https_proxy:
        handlers.append(urllib.request.ProxyHandler({"https": cfg.alerts.https_proxy}))
    opener = urllib.request.build_opener(*handlers)
    url = f"{cfg.alerts.telegram_api.rstrip('/')}/bot{token}/getUpdates"
    try:
        with opener.open(url, timeout=20) as resp:
            data = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise OdooGuardError(f"Telegram a refusé le jeton (HTTP {exc.code})", "command") from None
    except urllib.error.URLError as exc:
        raise OdooGuardError(f"Telegram injoignable depuis OdooGuard : {exc.reason}", "command") from None
    chats = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            chats[str(chat["id"])] = chat.get("title") or " ".join(
                x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or ""
    if not chats:
        return {"chats": {}, "message": "aucune conversation trouvée : envoyer d'abord un message au bot "
                                        "(ou dans le groupe où il a été ajouté), puis relancer"}
    return {"chats": chats, "message": "conversations trouvées : " + " ; ".join(
        f"{cid} ({name})" for cid, name in chats.items())}


# ------------------------------------------------------------------ comptes du tableau de bord
def job_user_save(cfg: Config, params: dict, actor: str) -> dict:
    from odooguard.web import auth
    name = str(params.get("name", ""))
    role = params.get("role")
    if not auth.valid_username(name):
        raise OdooGuardError("nom invalide (2 à 32 caractères : lettres, chiffres, . _ -)", "usage")
    if role not in auth.ROLES:
        raise OdooGuardError("rôle : lecteur, operateur ou admin", "usage")
    accounts = auth.read_accounts(cfg.web.users_file)
    new_hash = take_secret(cfg, params.get("hash_token"))
    if name not in accounts and not new_hash:
        raise OdooGuardError("mot de passe obligatoire pour un nouveau compte", "usage")
    if new_hash and not new_hash.startswith("pbkdf2_sha256$"):
        raise OdooGuardError("empreinte de mot de passe invalide", "usage")
    if name in accounts and accounts[name]["role"] == "admin" and role != "admin":
        if sum(1 for a in accounts.values() if a["role"] == "admin") <= 1:
            raise OdooGuardError("impossible de retirer le rôle admin au dernier administrateur", "usage")
        if name == actor:
            raise OdooGuardError("on ne peut pas se retirer soi-même le rôle admin", "usage")
    created = name not in accounts
    accounts[name] = {"role": role, "hash": new_hash or accounts[name]["hash"]}
    auth.write_accounts(cfg.web.users_file, accounts)
    what = "créé" if created else ("modifié, mot de passe changé" if new_hash else "modifié")
    return {"name": name, "role": role, "message": f"compte {name} {what} (rôle {role})"}


def job_user_delete(cfg: Config, params: dict, actor: str) -> dict:
    from odooguard.web import auth
    name = str(params.get("name", ""))
    accounts = auth.read_accounts(cfg.web.users_file)
    if name not in accounts:
        raise OdooGuardError(f"compte inconnu : {name}", "usage")
    if params.get("confirm") != name:
        raise OdooGuardError(f"confirmation : retaper exactement « {name} »", "usage")
    if name == actor:
        raise OdooGuardError("on ne peut pas supprimer son propre compte", "usage")
    if accounts[name]["role"] == "admin" and sum(1 for a in accounts.values() if a["role"] == "admin") <= 1:
        raise OdooGuardError("impossible de supprimer le dernier administrateur", "usage")
    del accounts[name]
    auth.write_accounts(cfg.web.users_file, accounts)
    return {"name": name, "message": f"compte {name} supprimé (sessions ouvertes fermées)"}


# ------------------------------------------------------------------ surveillance
UNIT_RE = re.compile(r"^[A-Za-z0-9@._:-]{1,100}$")


def validate_monitoring(p: dict) -> dict:
    def num(key, lo, hi, cast=float):
        try:
            v = cast(str(p.get(key, "")).replace(",", "."))
        except ValueError:
            raise OdooGuardError(f"{key} : nombre attendu", "usage") from None
        if not lo <= v <= hi:
            raise OdooGuardError(f"{key} : entre {lo} et {hi}", "usage")
        return v
    warn = num("disk_warn_pct", 1, 99)
    crit = num("disk_crit_pct", 2, 100)
    if warn >= crit:
        raise OdooGuardError("le seuil critique doit être supérieur au seuil d'avertissement", "usage")
    units = _split(p.get("services_local"))
    if not 1 <= len(units) <= 20:
        raise OdooGuardError("services OdooGuard surveillés : de 1 à 20 noms", "usage")
    for u in units:
        if not UNIT_RE.match(u):
            raise OdooGuardError(f"nom de service invalide : {u}", "usage")
    units = [u if "." in u else u + ".service" for u in units]
    return {"disk_warn_pct": warn, "disk_crit_pct": crit, "services_local": units,
            "security": bool(p.get("security")),
            "failed_logins_warn": int(num("failed_logins_warn", 1, 10000, int)),
            "failed_logins_window": int(num("failed_logins_window", 1, 1440, int)),
            "notify_accounts": bool(p.get("notify_accounts")),
            "keep_days": int(num("keep_days", 7, 3650, int))}


def job_monitoring_save(cfg: Config, params: dict) -> dict:
    v = validate_monitoring(params)
    write_section(cfg.path, "monitoring", {
        "disk_warn_pct": f"{v['disk_warn_pct']:g}", "disk_crit_pct": f"{v['disk_crit_pct']:g}",
        "services_local": ", ".join(v["services_local"]),
        "security": "yes" if v["security"] else "no",
        "failed_logins_warn": v["failed_logins_warn"], "failed_logins_window": v["failed_logins_window"],
        "notify_accounts": "yes" if v["notify_accounts"] else "no", "keep_days": v["keep_days"]},
        comment="Surveillance - modifié depuis le tableau de bord")
    return {**v, "message": f"surveillance enregistrée (disques : {v['disk_warn_pct']:g} % / "
                            f"{v['disk_crit_pct']:g} %) ; prise en compte au prochain relevé (≤ 5 min)"}


# ------------------------------------------------------------------ bases protégées
DB_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}$")


def validate_databases(names) -> list[str]:
    dbs = []
    for n in names if isinstance(names, list) else _split(names):
        n = str(n).strip()
        if not n:
            continue
        if not DB_RE.match(n) or n in ("postgres", "template0", "template1"):
            raise OdooGuardError(f"nom de base invalide : {n}", "usage")
        if n not in dbs:
            dbs.append(n)
    if not 1 <= len(dbs) <= 20:
        raise OdooGuardError("choisir de 1 à 20 bases", "usage")
    return dbs


def job_databases_save(cfg: Config, params: dict) -> dict:
    dbs = validate_databases(params.get("databases") or [])
    before = list(cfg.databases)
    set_option(cfg.path, "odoo", "databases", ", ".join(dbs))
    return {"databases": dbs, "before": before,
            "message": "bases Odoo suivies : " + ", ".join(dbs) + (f" (avant : {', '.join(before)})"
                                                               if before != dbs else "")}


def job_nas_enable(cfg: Config, params: dict) -> dict:
    """Suspend ou réactive un NAS (copies, contrôles et alertes) sans le démonter ni le retirer."""
    name = str(params.get("name", ""))
    if name not in cfg.destinations:
        raise OdooGuardError(f"NAS inconnu : {name}", "usage")
    on = bool(params.get("enabled"))
    set_option(cfg.path, f"destination:{name}", "enabled", "yes" if on else "no")
    return {"name": name, "enabled": on,
            "message": f"NAS {name} " + ("réactivé : copies et contrôles reprennent" if on else
                                         "suspendu : plus de copie, de contrôle ni d'alerte jusqu'à réactivation")}


# ------------------------------------------------------------------ VM de test
URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]{1,253}(:\d{1,5})?(/[A-Za-z0-9._~/-]*)?$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,200}$")
LABEL_RE = re.compile(r"^[^\n\r<>\"]{1,60}$")


def validate_testvm(cfg: Config, p: dict) -> dict:
    v = {k: str(p.get(k, "")).strip() for k in ("label", "host", "odoo_url", "pg_version", "pg_cluster",
                                                 "odoo_service", "odoo_conf", "filestore_path")}
    checks = (("label", LABEL_RE, "nom affiché"), ("host", HOST_RE, "adresse (IP ou nom DNS)"),
              ("odoo_url", URL_RE, "URL d'Odoo (http://adresse:8069)"),
              ("pg_version", re.compile(r"^\d{2}$"), "version PostgreSQL (ex. 15)"),
              ("pg_cluster", re.compile(r"^[a-z0-9_]{1,32}$"), "instance PostgreSQL (ex. main)"),
              ("odoo_service", UNIT_RE, "service Odoo"), ("odoo_conf", PATH_RE, "fichier de configuration d'Odoo"),
              ("filestore_path", PATH_RE, "dossier filestore"))
    for key, rx, lib in checks:
        if not rx.match(v[key]):
            raise OdooGuardError(f"{lib} invalide : {v[key]!r}", "usage")
    prod = {cfg.source.host.lower()}
    if v["host"].lower() in prod or v["host"] in ("127.0.0.1", "localhost"):
        raise OdooGuardError("cette adresse est celle de la production (ou d'OdooGuard) : la VM de test "
                             "reçoit des copies neutralisées et des suppressions de bases de test", "usage")
    return v


def job_testvm_save(cfg: Config, params: dict) -> dict:
    name = cfg.restore_test.target
    v = validate_testvm(cfg, params)
    old = cfg.targets.get(name)
    write_section(cfg.path, f"target:{name}", {
        "label": v["label"], "host": v["host"],
        "ssh_user": old.ssh_user if old else "root",
        "ssh_key": old.ssh_key if old else "/etc/odooguard/ssh/id_agent",
        "pg_version": v["pg_version"], "pg_cluster": v["pg_cluster"], "odoo_service": v["odoo_service"],
        "odoo_conf": v["odoo_conf"], "filestore_path": v["filestore_path"], "odoo_url": v["odoo_url"],
        "is_production": "no"}, comment="VM de test - modifiée depuis le tableau de bord (jamais la production)")
    changed_host = old is not None and old.host != v["host"]
    msg = f"VM de test « {v['label']} » ({v['host']}) enregistrée"
    if changed_host:
        msg += (f" ; NOUVELLE ADRESSE : enregistrer son empreinte SSH sur OdooGuard "
                f"(sudo -u odooguard sh -c 'ssh-keyscan {v['host']} >> ~/.ssh/known_hosts') puis « Tester »")
    return {**v, "message": msg}


def job_testvm_test(cfg: Config) -> dict:
    """Connexion à l'agent de la VM de test : SSH, agent, Odoo, sécurité (ALLOW_TEST_DROP)."""
    from odooguard.remote import Agent
    t = cfg.target(cfg.restore_test.target)
    if t.is_production:
        raise OdooGuardError("la cible du test de restauration est marquée production : refusé", "usage")
    try:
        odoo = Agent(t).call("odoo-status", timeout=60).stdout.strip() or "?"
    except OdooGuardError as exc:
        text = str(exc)
        if "Host key verification failed" in text or "No ED25519 host key" in text or "known_hosts" in text:
            raise OdooGuardError(f"empreinte SSH de {t.host} inconnue : sur OdooGuard, lancer "
                                 f"sudo -u odooguard sh -c 'ssh-keyscan {t.host} >> ~/.ssh/known_hosts'",
                                 "command") from None
        if "Permission denied" in text:
            raise OdooGuardError(f"{t.host} refuse la clé d'OdooGuard : installer l'agent sur la VM "
                                 "(install/install-server01.sh avec OG_IP=<adresse d'OdooGuard>)", "command") from None
        raise OdooGuardError(f"VM de test injoignable : {text[-400:]}", "command") from None
    try:
        dbs = Agent(t).call_json("db-list-test", timeout=60).get("databases", [])
        drop = f"suppression des bases de test autorisée ({len(dbs)} base(s) ogtest_*)"
    except OdooGuardError:
        drop = "ATTENTION : ALLOW_TEST_DROP=yes absent de /etc/odooguard-agent.conf sur la VM"
    return {"odoo": odoo, "message": f"{t.label or t.name} ({t.host}) joignable ; Odoo : {odoo} ; {drop}"}
