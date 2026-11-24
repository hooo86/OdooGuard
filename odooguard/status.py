"""Calcul de l'état global : âge, taille, durée de la dernière sauvegarde
réussie par composant, retard du flux WAL, destinations, espace disque.

Sources de vérité :
* base       : ``pgbackrest info`` (une sauvegarde n'y figure qu'une fois complète) ;
* WAL        : agent sur server01 (pg_stat_replication) + tampon local ;
* fichiers   : ``restic snapshots`` (instantané créé seulement en fin de sauvegarde) ;
* copies NAS : historique SQLite (dernier succès vérifié par relecture).

Niveaux : ok < warning < critical. Le résultat est écrit dans status.json,
lu par le tableau de bord et par Zabbix (phase 2).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time

from odooguard import external
from odooguard.config import Config
from odooguard.files import Restic
from odooguard.history import History
from odooguard.pg import PgBackRest, wal_state
from odooguard.util import OdooGuardError, free_gb, io_probe

LEVELS = {"ok": 0, "warning": 1, "critical": 2, "unknown": 1}


def _level_for_age(age: float | None, max_age: float) -> str:
    if age is None:
        return "critical"
    if age > max_age:
        return "critical"
    if age > max_age * 0.8:
        return "warning"
    return "ok"


def _component(name: str, level: str, **kw) -> dict:
    return {"name": name, "level": level, **kw}


def compute(cfg: Config, history: History, *, query_source: bool = True) -> dict:
    now = time.time()
    comps: list[dict] = []

    # -------------------------------------------------- flux WAL (RPO base)
    wal = wal_state(cfg, query_source=query_source)
    lag = wal.get("lag_seconds")
    wal_level = "ok" if wal["ok"] else ("critical" if lag is None or lag > cfg.max_age["wal"]
                                        else "warning")
    if wal.get("slot_wal_status") in ("unreserved", "lost"):
        wal_level = "critical"
    comps.append(_component("wal", wal_level, age=lag, details=wal,
                            message=f"retard WAL {lag:.0f} s" if lag is not None else "aucun WAL reçu"))

    # -------------------------------------------------- pgBackRest
    db_age = None
    try:
        info = PgBackRest(cfg).info()
        last = info.last()
        last_full = info.last(("full",))
        db_age = now - last.stop if last else None
        full_age = now - last_full.stop if last_full else None
        level = max(_level_for_age(db_age, cfg.max_age["db_any"]),
                    _level_for_age(full_age, cfg.max_age["db_full"]), key=LEVELS.get)
        if info.status_code not in (0,):
            level = "critical"
        comps.append(_component(
            "db", level, age=db_age,
            last_label=last.label if last else None, last_type=last.type if last else None,
            size=last.size if last else None, repo_size=last.repo_size if last else None,
            added=last.repo_delta if last else None, duration=last.duration if last else None,
            full_age=full_age, pitr_from=info.backups[0].start if info.backups else None,
            wal_max=info.wal_max, message=info.status_message))
    except OdooGuardError as exc:
        comps.append(_component("db", "critical", age=None, message=str(exc)[:500]))

    # -------------------------------------------------- restic
    files_age = None
    try:
        snaps = Restic(cfg).snapshots()
        if snaps:
            s = snaps[-1]
            t = dt.datetime.fromisoformat(s["time"].replace("Z", "+00:00")).timestamp()
            files_age = now - t
            summary = s.get("summary") or {}
            run_row = history.last("files", status="success")
            comps.append(_component(
                "files", _level_for_age(files_age, cfg.max_age["files"]), age=files_age,
                last_label=s["short_id"], size=summary.get("total_bytes_processed"),
                added=summary.get("data_added"),
                duration=(run_row["ended_at"] - run_row["started_at"]) if run_row else None,
                snapshots=len(snaps), message="ok"))
        else:
            comps.append(_component("files", "critical", age=None, message="aucun instantané"))
    except OdooGuardError as exc:
        comps.append(_component("files", "critical", age=None, message=str(exc)[:500]))

    # -------------------------------------------------- copies NAS
    for dest in cfg.destinations.values():
        if not dest.enabled:
            continue
        # Accessibilité testée à chaque passage (toutes les 5 min) : NAS absent,
        # montage figé ou erreur d'E/S => alerte bien avant la copie de la nuit.
        try:
            if dest.share and dest.mountpoint and not os.path.ismount(dest.mountpoint):
                # NAS géré depuis le tableau de bord : tentative de remontage automatique
                from odooguard import nas
                try:
                    nas.ensure_mounted(dest)
                except OdooGuardError:
                    pass
            if dest.mountpoint and not os.path.ismount(dest.mountpoint):
                raise OdooGuardError(f"{dest.mountpoint} non monté", "mount")
            io_probe(dest.path, size=64 * 1024)
            free = free_gb(dest.path)
            lvl = "warning" if free < dest.min_free_gb else "ok"
            comps.append(_component(f"nas:{dest.name}", lvl, free_gb=round(free, 1),
                                    message=f"accessible, {free:.0f} Gio libres"))
        except (OdooGuardError, OSError) as exc:
            comps.append(_component(f"nas:{dest.name}", "critical",
                                    category=getattr(exc, "category", "io"),
                                    message=f"inaccessible : {exc}"[:400]))
        ok = history.last(f"copy:{dest.name}", status="success")
        lastrun = history.last(f"copy:{dest.name}")
        age = now - ok["ended_at"] if ok else None
        level = _level_for_age(age, cfg.max_age["copy"])
        msg = "ok" if ok else "aucune copie réussie pour l'instant"
        if lastrun and lastrun["status"] == "failed":
            level = "critical" if lastrun.get("category") in ("io", "mount", "space") else max(
                level, "warning", key=LEVELS.get)
            msg = f"dernier essai en échec ({lastrun.get('category')}) : {lastrun.get('message', '')[:300]}"
        comps.append(_component(f"copy:{dest.name}", level, age=age,
                                size=ok.get("size_bytes") if ok else None,
                                duration=(ok["ended_at"] - ok["started_at"]) if ok else None,
                                message=msg))

    # -------------------------------------------------- disque local
    for label, path in (("disk:repo", cfg.repo_path), ("disk:staging", cfg.staging_dir)):
        try:
            free = free_gb(path)
            lvl = "critical" if free < cfg.min_free_gb else (
                "warning" if free < cfg.min_free_gb * 2 else "ok")
            comps.append(_component(label, lvl, free_gb=round(free, 1), message=f"{free:.0f} Gio libres"))
        except OSError as exc:
            comps.append(_component(label, "critical", message=str(exc)))

    # -------------------------------------------------- test de restauration hebdomadaire
    ok_t = history.last("restore-test", status="success")
    last_t = history.last("restore-test")
    t_age = now - ok_t["ended_at"] if ok_t else None
    t_level = _level_for_age(t_age, cfg.restore_test.max_age)
    t_msg = (ok_t.get("message") or "réussi") if ok_t else "aucun test de restauration réussi"
    if last_t and last_t["status"] == "failed":
        t_level, t_msg = "critical", f"dernier test en échec : {(last_t.get('message') or '')[:300]}"
    comps.append(_component("restore-test", t_level, age=t_age,
                            duration=(ok_t["ended_at"] - ok_t["started_at"]) if ok_t else None,
                            message=t_msg))

    # -------------------------------------------------- Veeam, copie hors ligne G:
    comps.extend(external.components(cfg, history, now))

    # -------------------------------------------------- chaîne d'alerte elle-même
    # Si l'e-mail ET Telegram échouent, personne n'est prévenu : on le signale
    # (tableau de bord, Zabbix) comme un problème à part entière.
    remote_channels = [c for c in cfg.alerts.channels if c != "journal"]
    if remote_channels:
        notes = [n for n in history.notifications(20) if now - n["ts"] < 24 * 3600]
        if notes:
            res = json.loads(notes[0]["channels"] or "{}")
            failed = [c for c in remote_channels if c in res and res[c] != "ok"]
            if failed and len(failed) == len([c for c in remote_channels if c in res]):
                comps.append(_component("alerting", "critical", message=(
                    "aucun canal d'alerte n'a pu envoyer le dernier message : "
                    + "; ".join(f"{c} {res[c]}" for c in failed))[:400]))
            elif failed:
                comps.append(_component("alerting", "warning", message="; ".join(
                    f"{c} {res[c]}" for c in failed)[:400]))
            else:
                comps.append(_component("alerting", "ok", message="canaux opérationnels"))

    # -------------------------------------------------- échecs récents non résolus
    for comp in ("db", "files"):
        lastrun = history.last(comp)
        if lastrun and lastrun["status"] == "failed":
            c = next(c for c in comps if c["name"] == comp)
            c["last_failure"] = {"at": lastrun["ended_at"], "category": lastrun["category"],
                                 "message": (lastrun["message"] or "")[:300]}
            if LEVELS[c["level"]] < 1:
                c["level"] = "warning"

    # Perte de données maximale actuelle, par nature de données
    known = [x for x in (lag, db_age) if x is not None]
    rpo_db = min(known) if known else None  # « jamais » tant qu'aucune sauvegarde n'existe
    rpo = {"db_seconds": rpo_db, "files_seconds": files_age}
    overall = max((c["level"] for c in comps), key=LEVELS.get, default="unknown")
    return {"generated_at": now, "site": cfg.site, "overall": overall, "rpo": rpo,
            "components": comps}


def record(cfg: Config, history: History, status: dict) -> None:
    """Écrit status.json (atomique) et les mesures de perte de données (RPO)."""
    tmp = cfg.status_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(status, fh, indent=2, default=str)
    os.chmod(tmp, 0o644)
    os.replace(tmp, cfg.status_file)
    # Copie publique (sans secret) lisible par l'agent Zabbix : /var/lib/odooguard
    # n'est pas accessible aux autres comptes.
    pub = cfg.web.public_status
    if pub and os.path.isdir(os.path.dirname(pub)):
        tmp = pub + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(status, fh, indent=2, default=str)
        os.chmod(tmp, 0o644)
        os.replace(tmp, pub)
    for key in ("db_seconds", "files_seconds"):
        val = status["rpo"].get(key)
        if val is not None:
            history.metric(f"rpo_{key}", val, status["generated_at"])
    history.purge_metrics(time.time() - 400 * 86400)


def rpo_report(history: History, days: float) -> dict:
    since = time.time() - days * 86400
    out = {}
    for key in ("db_seconds", "files_seconds"):
        mx, n = history.metric_max(f"rpo_{key}", since)
        out[key] = {"max": mx, "samples": n}
    return {"days": days, **out}
