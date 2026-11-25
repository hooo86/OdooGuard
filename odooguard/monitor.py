"""Surveillance au-delà des sauvegardes : espace disque, services, sécurité d'Odoo.

Appelé toutes les 5 minutes par ``odooguard status --record --check`` (même
passage que l'état des sauvegardes), donc mêmes alertes sans rafale (``Alerter``).

* **Stockage** : OdooGuard (système, dépôts, zone de restauration), chaque NAS monté,
  serveur Odoo (système, PostgreSQL, filestore, journaux) via l'agent.
  Seuils en pourcentage d'occupation : [monitoring] disk_warn_pct / disk_crit_pct.
* **Services** : services OdooGuard importants (local, systemctl) et, sur le serveur
  Odoo, Odoo, PostgreSQL, serveur web, cron et ssh (liste établie par l'agent).
* **Sécurité Odoo** : connexions réussies et échouées lues dans le journal d'Odoo
  (lignes « Login failed / Login successful », niveau de journal info), et comptes
  créés, supprimés, archivés ou réactivés (comparaison avec le relevé précédent).

L'agent ne fait que LIRE (verbe « monitor ») ; la position de lecture du journal
est conservée côté OdooGuard (table kv), pas sur le serveur Odoo.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time

from odooguard.config import Config
from odooguard.history import History
from odooguard.util import OdooGuardError

log = logging.getLogger("odooguard")

GROUP = "monitor"  # composants affichés dans les encarts dédiés, pas dans le tableau des sauvegardes
ODOO_HOST = "odoo"
LOCAL_HOST = "odooguard"
HOST_FR = {ODOO_HOST: "Serveur Odoo", LOCAL_HOST: "OdooGuard", "nas": "NAS"}
ACCOUNT_FR = {"user_created": "compte créé", "user_deleted": "compte supprimé",
              "user_archived": "compte archivé (désactivé)", "user_reactivated": "compte réactivé",
              "user_renamed": "identifiant modifié"}


def _comp(name: str, level: str, label: str, message: str, **kw) -> dict:
    return {"name": name, "level": level, "label": label, "message": message, "group": GROUP, **kw}


def disk_level(cfg: Config, pct: float) -> str:
    m = cfg.monitoring
    return "critical" if pct >= m.disk_crit_pct else ("warning" if pct >= m.disk_warn_pct else "ok")


def disk_entry(cfg: Config, host: str, label: str, mount: str, total: int, free: int,
               used: int | None = None, **extra) -> dict:
    used = total - free if used is None else used
    pct = round(100.0 * used / total, 1) if total else 0.0
    return {"host": host, "label": label, "mount": mount, "total": total, "free": free,
            "used": used, "pct": pct, "level": disk_level(cfg, pct), **extra}


# ------------------------------------------------------------------ OdooGuard (local)
def local_disks(cfg: Config) -> list[dict]:
    out, seen = [], set()
    for label, path in (("système", "/"), ("dépôts de sauvegarde", cfg.repo_path),
                        ("zone de restauration", cfg.staging_dir), ("état", cfg.state_dir)):
        try:
            dev = os.stat(path).st_dev
            st = os.statvfs(path)
        except OSError:
            continue
        if dev in seen:
            continue
        seen.add(dev)
        mnt = os.path.realpath(path)
        while not os.path.ismount(mnt):
            mnt = os.path.dirname(mnt)
        out.append(disk_entry(cfg, LOCAL_HOST, label, mnt, st.f_blocks * st.f_frsize,
                              st.f_bavail * st.f_frsize,
                              (st.f_blocks - st.f_bfree) * st.f_frsize))
    return out


def nas_disks(cfg: Config, history: History) -> list[dict]:
    out = []
    for dest in cfg.destinations.values():
        if not dest.enabled:
            continue
        if dest.mountpoint and not os.path.ismount(dest.mountpoint):
            continue  # NAS absent : déjà signalé par le composant nas:<nom>
        try:
            st = os.statvfs(dest.path)
        except OSError:
            continue
        last = history.last(f"copy:{dest.name}", status="success")
        out.append(disk_entry(cfg, "nas", dest.name, dest.share or dest.path,
                              st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize,
                              (st.f_blocks - st.f_bfree) * st.f_frsize,
                              odooguard_bytes=last.get("size_bytes") if last else None))
    return out


def unit_state(unit: str) -> dict:
    try:
        r = subprocess.run(["systemctl", "show", unit, "-p",
                            "Id,LoadState,ActiveState,SubState,ActiveEnterTimestamp,NRestarts,Description"],
                           capture_output=True, text=True, timeout=20)
        s = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    except (OSError, subprocess.TimeoutExpired) as exc:
        s = {"ActiveState": "unknown", "Description": str(exc)}
    return {"unit": unit, "description": s.get("Description", ""), "load": s.get("LoadState"),
            "active": s.get("ActiveState"), "sub": s.get("SubState"),
            "since": s.get("ActiveEnterTimestamp", ""), "restarts": s.get("NRestarts")}


def service_level(svc: dict) -> str:
    if svc.get("active") in ("active", "reloading"):
        return "ok"
    if svc.get("active") == "activating":
        return "warning"
    return "critical"


# ------------------------------------------------------------------ sécurité Odoo
def process_auth(cfg: Config, history: History, auth: dict) -> int:
    events = [{"ts": e["ts"], "kind": "login_ok" if e.get("ok") else "login_failed",
               "db": e.get("db"), "login": e.get("login"), "ip": e.get("ip")}
              for e in auth.get("events") or []]
    n = history.security_add(events)
    history.kv_put("odoo_auth_cursor", auth.get("cursor") or "-")
    history.kv_put("odoo_auth_source", auth.get("source") or "")
    return n


def diff_users(db: str, before: dict | None, users: list[dict], now: float) -> list[dict]:
    """Événements de comptes entre deux relevés (aucun au tout premier relevé)."""
    if before is None:
        return []
    cur = {str(u["id"]): u for u in users}
    events = []
    for uid, u in cur.items():
        old = before.get(uid)
        if old is None:
            events.append({"ts": u.get("created") or now, "kind": "user_created", "db": db,
                           "login": u.get("login"), "actor": u.get("created_by"),
                           "detail": f"{u.get('name') or ''}{' (portail)' if u.get('share') else ''}"})
            continue
        if old.get("active") and not u.get("active"):
            events.append({"ts": u.get("written") or now, "kind": "user_archived", "db": db,
                           "login": u.get("login"), "actor": u.get("written_by"), "detail": u.get("name")})
        elif not old.get("active") and u.get("active"):
            events.append({"ts": u.get("written") or now, "kind": "user_reactivated", "db": db,
                           "login": u.get("login"), "actor": u.get("written_by"), "detail": u.get("name")})
        if old.get("login") != u.get("login"):
            events.append({"ts": u.get("written") or now, "kind": "user_renamed", "db": db,
                           "login": u.get("login"), "actor": u.get("written_by"),
                           "detail": f"ancien identifiant : {old.get('login')}"})
    for uid, old in before.items():
        if uid not in cur:
            events.append({"ts": now, "kind": "user_deleted", "db": db, "login": old.get("login"),
                           "detail": f"{old.get('name') or ''} (constaté au relevé)"})
    return events


def process_users(cfg: Config, history: History, users_by_db: dict, now: float) -> list[dict]:
    all_events = []
    for db, users in (users_by_db or {}).items():
        if users is None:
            continue  # base absente
        key = f"odoo_users:{db}"
        before = history.kv_get(key)
        events = diff_users(db, before, users, now)
        history.kv_put(key, {str(u["id"]): {k: u.get(k) for k in (
            "login", "name", "active", "share", "created", "created_by", "written", "written_by",
            "last_login")} for u in users})
        all_events += events
    history.security_add(all_events)
    return all_events


def notify_accounts(cfg: Config, history: History, events: list[dict]) -> None:
    if not events or not cfg.monitoring.notify_accounts:
        return
    from odooguard import notify
    lines = [f"- {ACCOUNT_FR.get(e['kind'], e['kind'])} : {e.get('login')} (base {e.get('db')}"
             + (f", par {e['actor']}" if e.get("actor") else "") + ")"
             + (f" - {e['detail']}" if e.get("detail") else "") for e in events]
    notify.dispatch(cfg, "warning", f"comptes Odoo : {len(events)} changement(s)",
                    "\n".join(lines) + "\n\nDétail : tableau de bord, page « Sécurité Odoo ».",
                    key="odoo-accounts", history=history)


def security_component(cfg: Config, history: History, now: float) -> dict:
    m = cfg.monitoring
    since = now - m.failed_logins_window * 60
    n = history.security_count(since, "login_failed")
    level = "ok"
    if n >= m.failed_logins_warn * 3:
        level = "critical"
    elif n >= m.failed_logins_warn:
        level = "warning"
    msg = f"{n} connexion(s) Odoo échouée(s) en {m.failed_logins_window} min"
    if level != "ok":
        top = history.security_top_ips(since, limit=3)
        msg += " (seuil " + str(m.failed_logins_warn) + ") : " + ", ".join(
            f"{t['ip']} ×{t['n']}" for t in top) + " - tentative d'intrusion possible"
    return _comp("securite:odoo", level, "Connexions Odoo échouées", msg, failed_window=n)


# ------------------------------------------------------------------ point d'entrée
def collect(cfg: Config, history: History, *, query_source: bool = True,
            previous: dict | None = None) -> tuple[list[dict], dict]:
    """Retourne (composants pour l'alerte, données pour le tableau de bord)."""
    now = time.time()
    if not query_source and previous and previous.get("monitor"):
        # Rafraîchissement local (après une action sur un NAS...) : on garde le dernier relevé
        comps = [c for c in previous.get("components", []) if c.get("group") == GROUP]
        return comps, previous["monitor"]
    comps: list[dict] = []
    data: dict = {"collected_at": now, "storage": [], "services": [], "failed_units": {},
                  "security": {}, "remote_error": None,
                  "thresholds": {"warn": f"{cfg.monitoring.disk_warn_pct:g}",
                                 "crit": f"{cfg.monitoring.disk_crit_pct:g}"}}

    data["storage"] += local_disks(cfg)
    data["storage"] += nas_disks(cfg, history)
    for svc in cfg.monitoring.services_local:
        s = unit_state(svc)
        s.update(host=LOCAL_HOST, level=service_level(s))
        data["services"].append(s)

    remote = None
    if query_source:
        from odooguard.remote import Agent
        cursor = history.kv_get("odoo_auth_cursor", "-") or "-"
        dbs = cfg.databases if cfg.monitoring.security else []
        try:
            remote = Agent(cfg.source).call_json("monitor", cursor, *dbs, timeout=180)
            if not remote.get("version"):
                raise OdooGuardError("réponse vide", "command")
        except Exception as exc:  # noqa: BLE001 - la surveillance ne doit jamais bloquer l'état
            text = str(exc)
            if "verbe inconnu" in text:
                text = "agent du serveur Odoo trop ancien : le mettre à jour (verbe « monitor »)"
            data["remote_error"] = text[:400]
            comps.append(_comp("surveillance:odoo", "warning", "Surveillance du serveur Odoo",
                               f"relevé impossible : {text[:300]}"))
    if remote:
        comps.append(_comp("surveillance:odoo", "ok", "Surveillance du serveur Odoo", "relevé effectué"))
        for d in remote.get("disks", []):
            data["storage"].append(disk_entry(cfg, ODOO_HOST, d.get("label", ""), d["mount"],
                                              d["total"], d["free"], d.get("used")))
        for s in remote.get("services", []):
            s.update(host=ODOO_HOST, level=service_level(s))
            data["services"].append(s)
        data["failed_units"][ODOO_HOST] = remote.get("failed_units", [])
        if cfg.monitoring.security:
            try:
                process_auth(cfg, history, remote.get("auth") or {})
                acc = process_users(cfg, history, remote.get("users") or {}, now)
                notify_accounts(cfg, history, acc)
            except Exception as exc:  # noqa: BLE001
                log.warning("sécurité Odoo : %s", exc)
        history.security_purge(now - cfg.monitoring.keep_days * 86400)
    elif previous and previous.get("monitor"):
        # Serveur Odoo injoignable : on garde son dernier relevé (marqué comme ancien)
        old = previous["monitor"]
        for d in old.get("storage", []):
            if d.get("host") == ODOO_HOST:
                data["storage"].append({**d, "stale": True})
        for s in old.get("services", []):
            if s.get("host") == ODOO_HOST:
                data["services"].append({**s, "stale": True})

    for d in data["storage"]:
        if d.get("stale"):
            continue
        name = f"stockage:{d['host']}:{d['label'] if d['host'] == 'nas' else d['mount']}"
        host = HOST_FR.get(d["host"], d["host"])
        comps.append(_comp(name, d["level"], f"Espace disque {host} - {d['label']} ({d['mount']})",
                           f"{d['pct']:.0f} % occupé, {d['free'] / 1024 ** 3:.0f} Gio libres "
                           f"(seuils {cfg.monitoring.disk_warn_pct:g} % / {cfg.monitoring.disk_crit_pct:g} %)"))
    for s in data["services"]:
        if s.get("stale"):
            continue
        host = HOST_FR.get(s["host"], s["host"])
        state = s.get("active") or "inconnu"
        msg = f"{state} ({s.get('sub') or '-'})"
        if s.get("load") == "not-found":
            msg = "service introuvable sur ce serveur (nom à vérifier dans les réglages)"
        comps.append(_comp(f"service:{s['host']}:{s['unit']}", s["level"],
                           f"Service {s['unit']} ({host})", msg))
    if cfg.monitoring.security:
        comps.append(security_component(cfg, history, now))
        day = now - 86400
        data["security"] = {"failed_24h": history.security_count(day, "login_failed"),
                            "ok_24h": history.security_count(day, "login_ok"),
                            "source": history.kv_get("odoo_auth_source", "")}
    return comps, data


def read_previous(cfg: Config) -> dict | None:
    try:
        with open(cfg.status_file, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
