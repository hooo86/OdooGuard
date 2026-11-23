"""Composants extérieurs : Veeam Backup & Replication, copie hors ligne vers G:\\.

Ces outils tournent sous Windows. Un script PowerShell planifié
(``deploy/windows/Send-OdooGuardReport.ps1``) envoie après chaque exécution un
court rapport JSON à OdooGuard :

    POST https://odooguard:8443/api/v1/report/<nom>
    Authorization: Bearer <jeton propre à ce composant>
    {"status": "success|warning|failed", "finished_at": "2026-11-23T02:14:00+01:00",
     "size_bytes": 123, "duration_s": 3600, "message": "...", "details": {...}}

Le jeton donne uniquement le droit de déposer un rapport pour CE composant
(ni lecture, ni action). L'absence de rapport réussi depuis ``max_age`` est
elle-même une alerte : un script arrêté ou une tâche planifiée désactivée ne
passe pas inaperçu.
"""

from __future__ import annotations

import datetime as dt
import hmac
import time

from odooguard.config import Config, External
from odooguard.history import History

STATUSES = ("success", "warning", "failed")


class ReportError(ValueError):
    pass


def check_token(ext: External, presented: str) -> bool:
    if not ext.token_file or not presented:
        return False
    try:
        with open(ext.token_file, encoding="utf-8") as fh:
            expected = fh.read().strip()
    except OSError:
        return False
    return bool(expected) and hmac.compare_digest(expected.encode(), presented.encode())


def _ts(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ReportError(f"date invalide : {value!r}") from None
    if t.tzinfo is None:
        t = t.astimezone()
    return t.timestamp()


def validate(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ReportError("le rapport doit être un objet JSON")
    status = str(payload.get("status", "")).lower()
    if status not in STATUSES:
        raise ReportError(f"status doit valoir {', '.join(STATUSES)}")
    out = {"status": status, "finished_at": _ts(payload.get("finished_at")),
           "message": str(payload.get("message", ""))[:2000],
           "details": payload.get("details") if isinstance(payload.get("details"), (dict, list)) else {}}
    for key, typ in (("size_bytes", int), ("duration_s", float)):
        v = payload.get(key)
        if v in (None, ""):
            out[key] = None
            continue
        try:
            out[key] = typ(v)
        except (TypeError, ValueError):
            raise ReportError(f"{key} invalide") from None
    return out


def store(history: History, name: str, payload: dict, source_ip: str) -> dict:
    r = validate(payload)
    history.external_add(name, r["status"], finished_at=r["finished_at"], size=r["size_bytes"],
                         duration=r["duration_s"], message=r["message"], details=r["details"],
                         source_ip=source_ip)
    return r


def components(cfg: Config, history: History, now: float | None = None) -> list[dict]:
    now = now or time.time()
    out = []
    for ext in cfg.externals.values():
        if not ext.enabled:
            continue
        last = history.external_last(ext.name)
        good = [history.external_last(ext.name, s) for s in ("success", "warning")]
        good = [g for g in good if g]
        last_ok = max(good, key=lambda g: g["received_at"]) if good else None
        age = None
        if last_ok:
            age = now - (last_ok["finished_at"] or last_ok["received_at"])
        if last is None:
            level, msg = "critical", "aucun rapport reçu"
        elif last["status"] == "failed":
            level, msg = "critical", f"dernière exécution en échec : {last['message']}"
        elif age is None or age > ext.max_age:
            level, msg = "critical", "pas d'exécution réussie récente (script ou tâche planifiée arrêté ?)"
        elif last["status"] == "warning":
            level, msg = "warning", f"terminé avec avertissements : {last['message']}"
        elif age > ext.max_age * 0.8:
            level, msg = "warning", last["message"] or "ok"
        else:
            level, msg = "ok", last["message"] or "ok"
        out.append({"name": f"ext:{ext.name}", "label": ext.label, "level": level, "age": age,
                    "size": last_ok["size_bytes"] if last_ok else None,
                    "duration": last_ok["duration_s"] if last_ok else None,
                    "last_report": last["received_at"] if last else None,
                    "message": msg[:400]})
    return out
