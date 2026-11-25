"""Notifications : journal système, e-mail (SMTP) et Telegram.

Deux usages :

* **Événements** (``send``) : une sauvegarde ou une unité systemd vient
  d'échouer. Le message part immédiatement sur tous les canaux configurés.
* **États** (``Alerter.process``) : appelé toutes les 5 min avec l'état
  global. Pour éviter les rafales, on ne notifie que les changements :
  apparition d'un problème, aggravation ou atténuation, rappel toutes les
  ``repeat_hours`` tant qu'il persiste, puis « résolu ». Tous les
  changements d'un même passage sont regroupés en un seul message.

Les secrets (mot de passe SMTP, jeton du bot Telegram) sont lus dans des
fichiers 600 référencés par la configuration, jamais dans le code ni dans Git.
Le canal « journal » est toujours actif : même si l'e-mail et Telegram sont
en panne, l'alerte reste visible (journalctl -t odooguard-alert, Zabbix, tableau de bord).
"""

from __future__ import annotations

import json
import logging
import smtplib
import socket
import ssl
import syslog
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from odooguard.util import fmt_age

log = logging.getLogger("odooguard")

LEVELS = {"ok": 0, "warning": 1, "critical": 2}
LEVEL_FR = {"ok": "OK", "warning": "AVERTISSEMENT", "critical": "CRITIQUE"}
ICON = {"ok": "✅", "warning": "⚠️", "critical": "🛑"}
LEVEL_TO_SYSLOG = {"critical": syslog.LOG_CRIT, "warning": syslog.LOG_WARNING, "ok": syslog.LOG_INFO}

_cfg = None
_history = None


def configure(cfg, history) -> None:
    """Appelé au démarrage de la CLI / du service web."""
    global _cfg, _history
    _cfg, _history = cfg, history


def _read_secret(path: str) -> str:
    if not path:
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


# ====================================================================== canaux
class Channel:
    name = "?"

    def __init__(self, cfg):
        self.cfg = cfg
        self.a = cfg.alerts

    def send(self, level: str, title: str, body: str) -> None:  # pragma: no cover
        raise NotImplementedError


class JournalChannel(Channel):
    name = "journal"

    def send(self, level, title, body):
        syslog.openlog("odooguard-alert", 0, syslog.LOG_DAEMON)
        syslog.syslog(LEVEL_TO_SYSLOG.get(level, syslog.LOG_ERR),
                      f"[{LEVEL_FR.get(level, level)}] {title} | {body}"[:4000])


class EmailChannel(Channel):
    name = "email"

    def send(self, level, title, body):
        a = self.a
        if not (a.smtp_host and a.mail_from and a.mail_to):
            raise RuntimeError("SMTP non configuré ([smtp] host, from, to)")
        msg = EmailMessage()
        msg["Subject"] = f"[OdooGuard {self.cfg.site}] {LEVEL_FR.get(level, level)} : {title}"
        msg["From"] = a.mail_from
        msg["To"] = ", ".join(a.mail_to)
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="odooguard.local")
        msg["X-OdooGuard-Level"] = level
        msg.set_content(f"{title}\n\n{body}\n\n--\nOdooGuard - {socket.gethostname()}\n")
        ctx = ssl.create_default_context()
        if a.smtp_security == "ssl":
            server = smtplib.SMTP_SSL(a.smtp_host, a.smtp_port, timeout=20, context=ctx)
        else:
            server = smtplib.SMTP(a.smtp_host, a.smtp_port, timeout=20)
        try:
            if a.smtp_security == "starttls":
                server.starttls(context=ctx)
            if a.smtp_user:
                server.login(a.smtp_user, _read_secret(a.smtp_password_file))
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass


class TelegramChannel(Channel):
    name = "telegram"

    def send(self, level, title, body):
        a = self.a
        token = _read_secret(a.telegram_token_file)
        if not token or not a.telegram_chat_ids:
            raise RuntimeError("Telegram non configuré ([telegram] token_file, chat_ids)")
        text = f"{ICON.get(level, '')} OdooGuard {self.cfg.site} - {LEVEL_FR.get(level, level)}\n{title}\n\n{body}"
        handlers = []
        if a.https_proxy:
            handlers.append(urllib.request.ProxyHandler({"https": a.https_proxy}))
        opener = urllib.request.build_opener(*handlers)
        url = f"{a.telegram_api.rstrip('/')}/bot{token}/sendMessage"
        for chat in a.telegram_chat_ids:
            data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000],
                                           "disable_web_page_preview": "true"}).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            try:
                with opener.open(req, timeout=20) as resp:
                    payload = json.loads(resp.read().decode() or "{}")
            except urllib.error.HTTPError as exc:
                # Ne jamais faire figurer le jeton dans le message d'erreur
                raise RuntimeError(f"Telegram HTTP {exc.code} pour le chat {chat}") from None
            except urllib.error.URLError as exc:
                raise RuntimeError(f"Telegram injoignable : {exc.reason}") from None
            if not payload.get("ok"):
                raise RuntimeError(f"Telegram a refusé le message : {payload.get('description')}")


CHANNELS = {c.name: c for c in (JournalChannel, EmailChannel, TelegramChannel)}


def dispatch(cfg, level: str, title: str, body: str, *, key: str = "event",
             history=None, force_all: bool = False, only: str | None = None,
             category: str | None = None) -> dict:
    """Envoie sur chaque canal ; un canal en panne n'empêche pas les autres.

    ``only`` : un seul canal (message de test d'un canal précis, même non activé).
    """
    results: dict[str, str] = {}
    names = ["journal"] + [c for c in cfg.alerts.channels if c != "journal"]
    if only:
        names = [only]
    elif category and category in cfg.alerts.routes:
        # Réglage par type : seuls les canaux choisis pour ce type (le journal reçoit tout)
        names = ["journal"] + [c for c in names if c != "journal" and c in cfg.alerts.routes[category]]
    min_level = LEVELS.get(cfg.alerts.min_level, 1)
    for name in names:
        cls = CHANNELS.get(name)
        if cls is None:
            results[name] = "erreur : canal inconnu"
            continue
        if name != "journal" and not force_all and LEVELS.get(level, 2) < min_level and level != "ok":
            continue
        try:
            cls(cfg).send(level, title, body)
            results[name] = "ok"
        except Exception as exc:  # noqa: BLE001 - un canal ne doit jamais faire tomber l'appelant
            results[name] = f"erreur : {exc}"[:300]
            log.error("alerte %s non envoyée : %s", name, exc)
    if history is not None:
        try:
            history.notification_log(key, level, title, results, body)
        except Exception as exc:  # noqa: BLE001
            log.error("journal des notifications : %s", exc)
    return results


def send(level: str, title: str, body: str = "", *, key: str = "event", category: str = "echec") -> dict:
    """Alerte d'événement (échec d'une sauvegarde, d'une unité...)."""
    log.log(logging.ERROR if level == "critical" else logging.WARNING, "%s %s", title, body)
    if _cfg is None:
        JournalChannel.send(None, level, title, body)  # type: ignore[arg-type]
        return {"journal": "ok"}
    return dispatch(_cfg, level, title, body, key=key, history=_history, category=category)


def category_of(component: str) -> str:
    """Catégorie de notification d'un composant de l'état global."""
    if component.startswith(("stockage:", "disk:")):
        return "stockage"
    if component.startswith(("service:", "surveillance")):
        return "services"
    if component.startswith("securite:"):
        return "intrusion"
    return "etat"


CATEGORY_TITLE = {"etat": ("problème(s) de sauvegarde", "sauvegardes revenues à la normale"),
                  "stockage": ("alerte(s) d'espace disque", "espace disque revenu à la normale"),
                  "services": ("service(s) en panne", "services revenus à la normale"),
                  "intrusion": ("alerte de sécurité Odoo", "sécurité Odoo : retour à la normale")}


# ====================================================================== états
class Alerter:
    """Transforme l'état global (toutes les 5 min) en notifications sans rafale."""

    def __init__(self, cfg, history, now: float | None = None):
        self.cfg = cfg
        self.history = history
        self.now = now or time.time()

    def process(self, status: dict) -> dict | None:
        a = self.cfg.alerts
        min_level = LEVELS.get(a.min_level, 1)
        groups: dict[str, dict] = {}

        def grp(cat):
            return groups.setdefault(cat, {"new": [], "rem": [], "ok": [], "worst": "ok"})
        for comp in status.get("components", []):
            key, level = comp["name"], comp["level"]
            if level not in LEVELS:
                level = "warning"
            g = grp(category_of(key))
            cur = self.history.alert_get(key)
            active = cur is not None and cur["resolved_at"] is None
            msg = (comp.get("message") or "")[:300]
            if LEVELS[level] >= min_level:
                if not active or cur["level"] != level:
                    verb = "nouveau" if not active else (
                        "aggravé" if LEVELS[level] > LEVELS.get(cur["level"], 0) else "atténué")
                    g["new"].append(f"{ICON[level]} {key} [{LEVEL_FR[level]}, {verb}] : {msg}")
                    self.history.alert_put(key, level=level, message=msg, last_sent=self.now,
                                           sent_count=(cur["sent_count"] + 1 if active else 1),
                                           resolved_at=None,
                                           **({} if active else {"first_seen": self.now}))
                    g["worst"] = max(g["worst"], level, key=LEVELS.get)
                elif self.now - (cur["last_sent"] or 0) >= a.repeat_hours * 3600:
                    since = fmt_age(self.now - cur["first_seen"])
                    g["rem"].append(f"{ICON[level]} {key} [{LEVEL_FR[level]}, depuis {since}] : {msg}")
                    self.history.alert_put(key, message=msg, last_sent=self.now,
                                           sent_count=cur["sent_count"] + 1)
                    g["worst"] = max(g["worst"], level, key=LEVELS.get)
            elif active:
                since = fmt_age(self.now - cur["first_seen"])
                g["ok"].append(f"{ICON['ok']} {key} : résolu (problème apparu il y a {since})")
                self.history.alert_put(key, level="ok", resolved_at=self.now, message=msg)
        results = {}
        for cat, g in groups.items():
            if not (g["new"] or g["rem"] or (g["ok"] and a.send_resolved)):
                continue
            parts = []
            if g["new"]:
                parts.append("Changements :\n" + "\n".join(g["new"]))
            if g["rem"]:
                parts.append("Toujours en cours (rappel) :\n" + "\n".join(g["rem"]))
            if g["ok"] and a.send_resolved:
                parts.append("Résolus :\n" + "\n".join(g["ok"]))
            if cat == "etat":
                rpo = status.get("rpo") or {}
                parts.append(f"Perte de données max. actuelle : base {fmt_age(rpo.get('db_seconds'))}, "
                             f"fichiers {fmt_age(rpo.get('files_seconds'))}. "
                             f"État global : {LEVEL_FR.get(status.get('overall'), status.get('overall'))}.")
            n_bad = len(g["new"]) + len(g["rem"])
            bad_title, ok_title = CATEGORY_TITLE[cat]
            title = f"{n_bad} {bad_title}" if n_bad else ok_title
            results[cat] = dispatch(self.cfg, g["worst"] if n_bad else "ok", title, "\n\n".join(parts),
                                    key="status" if cat == "etat" else f"status:{cat}",
                                    history=self.history, category=cat)
        return results or None
