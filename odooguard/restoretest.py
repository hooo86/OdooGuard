"""Test de restauration automatique (hebdomadaire) sur la VM de test isolée.

Pour chaque base testée :
  1. restauration à un instant précis (par défaut « hier 16:00 ») sous le nom
     ``ogtest_<base>_<AAAAMMJJ_HHMM>`` sur la VM de test, filestore compris,
     copie NEUTRALISÉE (aucun courriel, crons désactivés) ;
  2. contrôles :
     - la restauration s'est arrêtée au bon instant (journal de rejeu des WAL) ;
     - aucune donnée postérieure à l'instant demandé (dernière activité Odoo) ;
     - comptages de contrôle non nuls et cohérents avec la production (± tolérance) ;
     - pièces jointes lisibles (échantillon de 200 fichiers du filestore) ;
     - la base est neutralisée ;
     - Odoo démarre et ``/web/login`` répond pour cette base ;
  3. mesure des durées (indicateur de temps de reprise) ;
  4. nettoyage : seules les ``keep_last`` bases de test les plus récentes restent ;
  5. rapport (JSON + texte) envoyé par e-mail / Telegram, visible au tableau de bord.

La production n'est jamais modifiée : seules des requêtes de comptage en
lecture y sont faites (``db-report``) pour comparaison.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

from odooguard import notify
from odooguard.audit import Auditor
from odooguard.config import Config
from odooguard.remote import Agent
from odooguard.restore import Restorer
from odooguard.util import OdooGuardError, fmt_age, fmt_time, parse_time

log = logging.getLogger("odooguard")

LEVELS = {"ok": 0, "warning": 1, "critical": 2}
CONTROL_KEYS = ("partners", "users", "companies", "account_moves", "sale_orders", "attachments")
RECOVERY_TIME_RE = re.compile(r"time (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?[+-]\d{2}(?::?\d{2})?)")


def test_db_name(db: str, when: dt.datetime) -> str:
    return f"ogtest_{db}_{when.astimezone():%Y%m%d_%H%M}"[:63]


def parse_recovery_time(line: str) -> dt.datetime | None:
    """Heure de la dernière transaction rejouée, extraite du journal PostgreSQL."""
    m = RECOVERY_TIME_RE.search(line or "")
    if not m:
        return None
    raw = m.group(1)
    if re.search(r"[+-]\d{2}$", raw):
        raw += ":00"
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def check_stop_point(line: str, when: dt.datetime) -> "Check":
    """Vérifie dans le journal de PostgreSQL que le rejeu s'est arrêté à l'instant demandé.

    Deux formes de message :
    * « recovery stopping before commit of transaction N, time T » : T est la
      PREMIÈRE transaction écartée, elle doit être postérieure à l'instant demandé ;
    * « last completed transaction was at log time T » : T est la DERNIÈRE
      transaction rejouée, elle doit être antérieure ou égale à l'instant demandé.
    """
    t = parse_recovery_time(line)
    name = "Arrêt au bon instant"
    if t is None:
        return Check(name, "warning", "instant de fin de rejeu non trouvé dans le journal")
    margin = dt.timedelta(seconds=1)
    if "stopping before" in line:
        if t + margin < when:
            return Check(name, "critical", f"rejeu arrêté trop tôt : transaction du {fmt_time(t)} écartée")
        return Check(name, "ok", f"première transaction écartée : {fmt_time(t)} (après {fmt_time(when)})")
    if t > when + margin:
        return Check(name, "critical", f"transaction du {fmt_time(t)} rejouée, après {fmt_time(when)}")
    return Check(name, "ok", f"dernière transaction rejouée : {fmt_time(t)}")


def _parse_odoo_ts(value) -> dt.datetime | None:
    """Odoo stocke ses dates en UTC sans fuseau."""
    if not value:
        return None
    try:
        t = dt.datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


class Check:
    def __init__(self, name: str, level: str, detail: str):
        self.name, self.level, self.detail = name, level, detail

    def as_dict(self) -> dict:
        return {"name": self.name, "level": self.level, "detail": self.detail}


def compare_controls(restored: dict, prod: dict | None, tolerance: float) -> Check:
    if not restored.get("partners") or not restored.get("users"):
        return Check("Comptages de contrôle", "critical",
                     f"base vide ou incomplète : partenaires={restored.get('partners')}, "
                     f"utilisateurs={restored.get('users')}")
    parts = [f"{k}={restored.get(k)}" for k in CONTROL_KEYS if restored.get(k) is not None]
    if not prod:
        return Check("Comptages de contrôle", "warning",
                     "comparaison avec la production impossible ; " + ", ".join(parts))
    drift = []
    for k in CONTROL_KEYS:
        r, p = restored.get(k), prod.get(k)
        if r is None or p in (None, 0):
            continue
        ecart = (p - r) / p
        # La production a avancé depuis l'instant restauré : on accepte un écart
        # positif jusqu'à la tolérance, et une légère baisse (suppressions).
        if ecart > tolerance or ecart < -0.05:
            drift.append(f"{k} : restauré {r} / production {p} ({ecart:+.1%})")
    if drift:
        return Check("Comptages de contrôle", "warning",
                     "écart supérieur à la tolérance : " + " ; ".join(drift))
    return Check("Comptages de contrôle", "ok",
                 ", ".join(parts) + f" (écart ≤ {tolerance:.0%} avec la production)")


def http_check(url: str, db: str, timeout: int) -> Check:
    full = f"{url.rstrip('/')}/web/login?db={db}"
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(full, timeout=30) as resp:
                body = resp.read(200_000).decode("utf-8", "replace")
                if resp.status == 200 and 'name="login"' in body:
                    return Check("Odoo démarre, /web/login répond", "ok", f"HTTP 200 sur {full}")
                last = f"HTTP {resp.status}, formulaire de connexion absent"
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            last = f"injoignable : {getattr(exc, 'reason', exc)}"
        time.sleep(5)
    return Check("Odoo démarre, /web/login répond", "critical", f"{full} : {last}")


class RestoreTester:
    def __init__(self, cfg: Config, auditor: Auditor, history):
        self.cfg = cfg
        self.auditor = auditor
        self.history = history
        self.settings = cfg.restore_test

    def run(self, databases: list[str] | None = None, point: str | None = None,
            send: bool = True) -> dict:
        s = self.settings
        target = self.cfg.target(s.target)
        if target.is_production:
            raise OdooGuardError("le test de restauration ne peut pas viser la production", "config")
        when = parse_time(point or s.point)
        dbs = databases or s.databases
        started = time.time()
        results = [self.test_one(db, when) for db in dbs]
        overall = max((r["level"] for r in results), key=LEVELS.get, default="critical")
        report = {"started_at": started, "ended_at": time.time(), "point": when.isoformat(),
                  "target": f"{target.name} ({target.host})", "level": overall,
                  "databases": results}
        self.save(report)
        if send:
            title = {"ok": "test de restauration RÉUSSI", "warning": "test de restauration réussi avec réserves",
                     "critical": "test de restauration EN ÉCHEC"}[overall]
            notify.dispatch(self.cfg, overall, title, format_report(report), key="restore-test",
                            history=self.history, force_all=True, category="test")
        return report

    def save(self, report: dict) -> str:
        d = os.path.join(self.cfg.state_dir, "reports")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"restore-test-{time.strftime('%Y%m%d-%H%M%S')}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False, default=str)
        report["file"] = path
        return path

    def test_one(self, db: str, when: dt.datetime) -> dict:
        s = self.settings
        target = self.cfg.target(s.target)
        agent = Agent(target)
        name = test_db_name(db, when)
        checks: list[Check] = []
        t0 = time.monotonic()
        out = {"db": db, "test_db": name, "checks": [], "durations": {}}
        try:
            # Une base de test du même nom (test relancé) est remplacée : c'est
            # un artefact de test, jamais une donnée (préfixe ogtest_ imposé par l'agent).
            agent.call("db-drop-test", name, timeout=900)
            rep = Restorer(self.cfg, self.auditor).restore_db(
                db=db, as_name=name, when=when, target_name=s.target, neutralize=True,
                with_filestore=True, force=False, confirm=None)
        except Exception as exc:  # noqa: BLE001 - le rapport doit tout de même partir
            checks.append(Check("Restauration", "critical", str(exc).splitlines()[0][:400]))
            out.update(level="critical", checks=[c.as_dict() for c in checks])
            out["durations"]["total_s"] = round(time.monotonic() - t0)
            return out

        out["durations"] = {"restauration_instance_s": rep.get("pg_restore_s"),
                            "transfert_base_s": rep.get("transfer_s")}
        out["recovery_point"] = rep.get("recovery_point")
        out["filestore_snapshot"] = rep.get("filestore")
        control = rep.get("control") or {}
        out["control"] = control
        checks.append(Check("Restauration", "ok",
                            f"base {name} restaurée au {fmt_time(when)} sur {target.name}"))

        # Instant atteint
        checks.append(check_stop_point(rep.get("recovery_point", ""), when))

        # Données postérieures à l'instant demandé ? Données trop anciennes ?
        last = _parse_odoo_ts(control.get("last_activity"))
        if last is None:
            checks.append(Check("Fraîcheur des données", "warning", "dernière activité inconnue"))
        elif last > when + dt.timedelta(minutes=1):
            checks.append(Check("Fraîcheur des données", "critical",
                                f"activité postérieure à l'instant demandé ({fmt_time(last)})"))
        elif (when - last).total_seconds() > s.max_staleness_days * 86400:
            checks.append(Check("Fraîcheur des données", "warning",
                                f"dernière activité il y a {fmt_age((when - last).total_seconds())} "
                                "avant l'instant restauré (sauvegarde ancienne ?)"))
        else:
            checks.append(Check("Fraîcheur des données", "ok",
                                f"dernière activité {fmt_time(last)}, "
                                f"{fmt_age((when - last).total_seconds())} avant l'instant restauré"))

        # Comptages comparés à la production (lecture seule)
        try:
            prod = Agent(self.cfg.source).call_json("db-report", db, timeout=600)
        except Exception as exc:  # noqa: BLE001
            log.warning("comptages de production indisponibles : %s", exc)
            prod = None
        out["control_production"] = prod
        checks.append(compare_controls(control, prod, s.tolerance))

        # Pièces jointes
        sampled, missing = control.get("attachments_sampled", 0), control.get("attachments_missing", 0)
        if missing:
            checks.append(Check("Pièces jointes lisibles", "critical",
                                f"{missing} fichier(s) absent(s) sur {sampled} contrôlés"))
        elif not sampled:
            checks.append(Check("Pièces jointes lisibles", "warning", "aucune pièce jointe à contrôler"))
        else:
            checks.append(Check("Pièces jointes lisibles", "ok", f"{sampled} fichier(s) contrôlé(s), aucun manquant"))

        # Neutralisation
        neutral = str(control.get("neutralized") or "").lower() in ("true", "1", "yes")
        checks.append(Check("Copie neutralisée", "ok" if neutral else "critical",
                            "database.is_neutralized = True" if neutral else
                            "la base n'est pas marquée neutralisée : NE PAS démarrer Odoo dessus"))

        # Odoo et /web/login
        if not neutral:
            checks.append(Check("Odoo démarre, /web/login répond", "critical", "non testé (copie non neutralisée)"))
        elif not target.odoo_url:
            checks.append(Check("Odoo démarre, /web/login répond", "warning",
                                f"odoo_url non configurée pour la cible {target.name}"))
        else:
            if s.start_odoo:
                try:
                    agent.call("odoo-start", timeout=300)
                except Exception as exc:  # noqa: BLE001
                    log.warning("odoo-start : %s", exc)
            t_http = time.monotonic()
            checks.append(http_check(target.odoo_url, name, s.http_timeout))
            out["durations"]["demarrage_odoo_s"] = round(time.monotonic() - t_http)

        out["durations"]["total_s"] = round(time.monotonic() - t0)
        self.cleanup(agent, keep=name)
        out["checks"] = [c.as_dict() for c in checks]
        out["level"] = max((c.level for c in checks), key=LEVELS.get)
        return out

    def cleanup(self, agent: Agent, keep: str) -> None:
        """Ne garde que les keep_last bases de test les plus récentes (dont la courante)."""
        try:
            names = agent.call_json("db-list-test").get("databases", [])
        except Exception as exc:  # noqa: BLE001
            log.warning("liste des bases de test : %s", exc)
            return
        others = sorted((n for n in names if n != keep), reverse=True)
        for old in others[max(0, self.settings.keep_last - 1):]:
            try:
                agent.call("db-drop-test", old, timeout=900)
                log.info("base de test %s supprimée", old)
            except Exception as exc:  # noqa: BLE001
                log.warning("suppression de %s : %s", old, exc)


ICON = {"ok": "✅", "warning": "⚠️", "critical": "❌"}


def format_report(report: dict) -> str:
    lines = [f"Instant restauré : {fmt_time(dt.datetime.fromisoformat(report['point']))}",
             f"Cible : {report['target']}",
             f"Durée totale : {fmt_age(report['ended_at'] - report['started_at'])}", ""]
    for r in report["databases"]:
        lines.append(f"{ICON[r['level']]} {r['db']} -> {r['test_db']}")
        for c in r["checks"]:
            lines.append(f"   {ICON[c['level']]} {c['name']} : {c['detail']}")
        d = r.get("durations") or {}
        if d:
            lines.append("   Durées : " + ", ".join(f"{k.replace('_s', '')} {fmt_age(v)}"
                                                   for k, v in d.items() if v is not None))
        lines.append("")
    if report.get("file"):
        lines.append(f"Rapport complet : {report['file']}")
    return "\n".join(lines)
