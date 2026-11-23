"""Exécution des travaux demandés depuis le tableau de bord.

Le service web est cloisonné (systemd : lecture seule sur les dépôts, aucune
élévation) : il ne fait qu'ENREGISTRER une demande dans la table ``jobs``.
Le service ``odooguard-jobs`` (ce module) la prend en charge, RE-VALIDE les
droits et les paramètres, puis l'exécute avec exactement le même code que la
ligne de commande (mêmes confirmations, mêmes verrous, même audit).
Le journal de chaque travail est écrit dans ``<state_dir>/jobs/<id>.log`` et
affiché en direct dans l'assistant.

Rôles (fichier des comptes web) :
  lecteur    consultation uniquement
  operateur  copies de restauration SANS écrasement (nouveau nom, VM de test), test de restauration
  admin      en plus : remplacement d'une base existante, y compris en production
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time

from odooguard.audit import Auditor
from odooguard.config import Config
from odooguard.history import History
from odooguard.restore import Restorer, validate_db_name
from odooguard.util import OdooGuardError, file_lock

log = logging.getLogger("odooguard")

ROLES = ("lecteur", "operateur", "admin")
ROLE_RANK = {r: i for i, r in enumerate(ROLES)}


def can(role: str, needed: str) -> bool:
    return ROLE_RANK.get(role, -1) >= ROLE_RANK[needed]


def validate_restore_params(cfg: Config, role: str, p: dict) -> dict:
    """Contrôle complet d'une demande de restauration (web ET worker)."""
    db = validate_db_name(str(p.get("db", "")))
    if db not in cfg.databases:
        raise OdooGuardError(f"base source inconnue : {db}", "usage")
    as_name = validate_db_name(str(p.get("as_name") or db))
    target = cfg.target(str(p.get("target", "")))
    when = p.get("when")
    if when:
        when = dt.datetime.fromisoformat(when)
        if when.tzinfo is None:
            when = when.astimezone()
        if when > dt.datetime.now(dt.timezone.utc):
            raise OdooGuardError("l'instant demandé est dans le futur", "usage")
    replace = bool(p.get("replace"))
    if not can(role, "operateur"):
        raise OdooGuardError("droits insuffisants (rôle opérateur requis)", "usage")
    if replace and not can(role, "admin"):
        raise OdooGuardError("remplacer une base existante exige le rôle admin", "usage")
    if target.is_production and as_name == db and not replace:
        raise OdooGuardError("restaurer sous le même nom en production remplace la base : "
                             "cocher « remplacer » (rôle admin)", "usage")
    neutralize = p.get("neutralize", "auto")
    if neutralize not in ("auto", "yes", "no"):
        raise OdooGuardError("neutralisation : auto, yes ou no", "usage")
    if neutralize == "no" and not (target.is_production and as_name == db):
        # Une copie non neutralisée enverrait des courriels réels aux clients.
        raise OdooGuardError("une copie (autre nom ou VM de test) est toujours neutralisée", "usage")
    confirm = p.get("confirm") or None
    if replace and confirm != as_name:
        raise OdooGuardError(f"confirmation : retaper exactement « {as_name} »", "usage")
    return {"db": db, "as_name": as_name, "target": target.name, "when": when,
            "replace": replace, "neutralize": {"auto": None, "yes": True, "no": False}[neutralize],
            "with_filestore": bool(p.get("with_filestore", True)), "confirm": confirm}


class _JobLog(logging.Handler):
    def __init__(self, path: str):
        super().__init__(logging.INFO)
        self.fh = open(path, "a", encoding="utf-8")
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.fh.write(self.format(record) + "\n")
        self.fh.flush()

    def close(self):
        self.fh.close()
        super().close()


def run_job(cfg: Config, history: History, job: dict) -> None:
    jobs_dir = os.path.join(cfg.state_dir, "jobs")
    os.makedirs(jobs_dir, exist_ok=True)
    log_path = os.path.join(jobs_dir, f"{job['id']}.log")
    history.job_update(job["id"], log_path=log_path)
    handler = _JobLog(log_path)
    lg = logging.getLogger("odooguard")
    lg.addHandler(handler)
    previous_level = lg.level
    if lg.getEffectiveLevel() > logging.INFO:
        lg.setLevel(logging.INFO)  # le journal du travail doit tout montrer
    os.environ["ODOOGUARD_ACTOR"] = job["user"]
    auditor = Auditor(history, cfg.audit_log)
    params = json.loads(job["params"])
    try:
        log.info("travail n°%s (%s) demandé par %s", job["id"], job["kind"], job["user"])
        if job["kind"] == "restore-db":
            v = validate_restore_params(cfg, job["role"], params)
            log.info("restauration de %s -> %s sur %s, instant : %s", v["db"], v["as_name"],
                     v["target"], v["when"] or "dernier état")
            with file_lock(cfg.lock_dir, "restore", wait=0):
                run_id = history.start("restore", "db")
                try:
                    result = Restorer(cfg, auditor).restore_db(
                        db=v["db"], as_name=v["as_name"], when=v["when"], target_name=v["target"],
                        neutralize=v["neutralize"], with_filestore=v["with_filestore"],
                        force=v["replace"], confirm=v["confirm"])
                except Exception as exc:
                    history.finish(run_id, "failed", message=str(exc), category=getattr(exc, "category", "error"))
                    raise
                history.finish(run_id, "success", message=f"{v['db']} -> {v['as_name']} ({v['target']})",
                               details=result)
        elif job["kind"] == "restore-test":
            if not can(job["role"], "operateur"):
                raise OdooGuardError("droits insuffisants (rôle opérateur requis)", "usage")
            from odooguard.restoretest import RestoreTester
            with file_lock(cfg.lock_dir, "restore", wait=0):
                run_id = history.start("restore-test", "manuel")
                report = RestoreTester(cfg, auditor, history).run(databases=params.get("databases"))
                history.finish(run_id, "failed" if report["level"] == "critical" else "success",
                               category="restore-test" if report["level"] == "critical" else None,
                               message=f"test {report['level']}", details=report)
                result = report
        else:
            raise OdooGuardError(f"type de travail inconnu : {job['kind']}", "usage")
        log.info("travail terminé avec succès")
        history.job_update(job["id"], status="success", ended_at=time.time(),
                           result=json.dumps(result, ensure_ascii=False, default=str))
    except Exception as exc:  # noqa: BLE001
        log.error("échec : %s", exc)
        history.job_update(job["id"], status="failed", ended_at=time.time(), error=str(exc)[:4000])
    finally:
        os.environ.pop("ODOOGUARD_ACTOR", None)
        lg.setLevel(previous_level)
        lg.removeHandler(handler)
        handler.close()


def worker(cfg: Config, history: History, interval: float = 3.0, once: bool = False) -> None:
    n = history.jobs_fail_stale()
    if n:
        log.warning("%d travail(aux) interrompu(s) marqué(s) en échec", n)
    while True:
        job = history.job_claim()
        if job:
            run_job(cfg, history, job)
            continue
        if once:
            return
        time.sleep(interval)
