import datetime as dt
import json

import pytest

from odooguard import jobs, restoretest
from odooguard.history import History
from odooguard.util import OdooGuardError

UTC = dt.timezone.utc
WHEN = dt.datetime(2026, 11, 22, 16, 0, tzinfo=UTC)


# ------------------------------------------------------------------ restoretest
def test_test_db_name():
    n = restoretest.test_db_name("LNHC_Prod_2025", WHEN)
    assert n.startswith("ogtest_LNHC_Prod_2025_20261122_") and len(n) <= 63


@pytest.mark.parametrize("line,level", [
    ("LOG:  recovery stopping before commit of transaction 733, time 2026-11-22 16:00:05.1+00", "ok"),
    ("LOG:  recovery stopping before commit of transaction 733, time 2026-11-22 15:40:00+00", "critical"),
    ("LOG:  last completed transaction was at log time 2026-11-22 15:59:58.3+00", "ok"),
    ("LOG:  last completed transaction was at log time 2026-11-22 16:30:00+00", "critical"),
    ("", "warning"),
])
def test_stop_point(line, level):
    assert restoretest.check_stop_point(line, WHEN).level == level


def test_compare_controls():
    prod = {"partners": 1000, "users": 50, "account_moves": 20000, "attachments": 5000}
    ok = {"partners": 990, "users": 50, "account_moves": 19900, "attachments": 4990}
    assert restoretest.compare_controls(ok, prod, 0.10).level == "ok"
    old = {**ok, "account_moves": 12000}
    c = restoretest.compare_controls(old, prod, 0.10)
    assert c.level == "warning" and "account_moves" in c.detail
    assert restoretest.compare_controls({"partners": 0, "users": 1}, prod, 0.1).level == "critical"
    assert restoretest.compare_controls(ok, None, 0.1).level == "warning"


def test_format_report():
    rep = {"started_at": 0, "ended_at": 120, "point": WHEN.isoformat(), "target": "testvm (1.2.3.4)",
           "level": "ok", "databases": [{"db": "A", "test_db": "ogtest_A", "level": "ok",
                                         "checks": [{"name": "Restauration", "level": "ok", "detail": "x"}],
                                         "durations": {"total_s": 100}}]}
    txt = restoretest.format_report(rep)
    assert "ogtest_A" in txt and "Restauration" in txt


class FakeAgent:
    calls = []
    report = {}
    prod = {}

    def __init__(self, target):
        self.target = target

    def call(self, verb, *a, **k):
        FakeAgent.calls.append((self.target.name, verb, *a))

    def call_json(self, verb, *a, **k):
        FakeAgent.calls.append((self.target.name, verb, *a))
        if verb == "db-report":
            return FakeAgent.prod
        if verb == "db-list-test":
            return {"databases": ["ogtest_A_20261101_1600", "ogtest_A_20261108_1600", "ogtest_A_20261122_1600"]}
        return {}


def _tester(cfg, monkeypatch, control, http_level="ok"):
    from odooguard.audit import Auditor
    FakeAgent.calls = []
    FakeAgent.prod = {"partners": 100, "users": 5, "attachments": 10}

    class FakeRestorer:
        def __init__(self, *a):
            pass

        def restore_db(self, **kw):
            assert kw["neutralize"] is True and kw["target_name"] == "testvm"
            return {"pg_restore_s": 30, "transfer_s": 20, "control": control,
                    "recovery_point": "LOG:  last completed transaction was at log time 2026-11-22 15:59:00+00"}
    monkeypatch.setattr(restoretest, "Agent", FakeAgent)
    monkeypatch.setattr(restoretest, "Restorer", FakeRestorer)
    monkeypatch.setattr(restoretest, "http_check",
                        lambda url, db, t: restoretest.Check("Odoo démarre, /web/login répond", http_level, url))
    cfg.targets["testvm"].odoo_url = "http://vm:8069"
    h = History(cfg.history_db)
    return restoretest.RestoreTester(cfg, Auditor(h, cfg.audit_log), h)


GOOD = {"partners": 99, "users": 5, "attachments": 10, "attachments_sampled": 10, "attachments_missing": 0,
        "neutralized": "True", "last_activity": "2026-11-22T15:30:00"}


def test_restore_test_success_and_cleanup(cfg, monkeypatch):
    t = _tester(cfg, monkeypatch, GOOD)
    t.settings.keep_last = 1
    r = t.test_one("A", WHEN)
    assert r["level"] == "ok", r["checks"]
    drops = [c for c in FakeAgent.calls if c[1] == "db-drop-test"]
    # 1 suppression préalable du nom courant + 2 anciennes bases (keep_last=1)
    assert {c[2] for c in drops} >= {"ogtest_A_20261101_1600", "ogtest_A_20261108_1600"}
    assert ("odoo", "db-report", "A") in FakeAgent.calls  # comparaison en lecture sur la production


@pytest.mark.parametrize("change,check", [
    ({"attachments_missing": 3}, "Pièces jointes lisibles"),
    ({"neutralized": None}, "Copie neutralisée"),
    ({"last_activity": "2026-11-22T17:00:00"}, "Fraîcheur des données"),
    ({"partners": 0}, "Comptages de contrôle"),
])
def test_restore_test_critical_cases(cfg, monkeypatch, change, check):
    r = _tester(cfg, monkeypatch, {**GOOD, **change}).test_one("A", WHEN)
    assert r["level"] == "critical"
    assert any(c["name"] == check and c["level"] == "critical" for c in r["checks"])


def test_restore_test_http_failure(cfg, monkeypatch):
    r = _tester(cfg, monkeypatch, GOOD, http_level="critical").test_one("A", WHEN)
    assert r["level"] == "critical"


def test_restore_test_never_on_production(cfg, monkeypatch):
    t = _tester(cfg, monkeypatch, GOOD)
    t.settings.target = "odoo"
    with pytest.raises(OdooGuardError, match="production"):
        t.run()


def test_run_saves_and_sends(cfg, monkeypatch):
    sent = []
    monkeypatch.setattr(restoretest.notify, "dispatch", lambda *a, **k: sent.append((a, k)))
    t = _tester(cfg, monkeypatch, GOOD)
    rep = t.run(databases=["A"], point="2026-11-22 16:00")
    assert rep["level"] == "ok" and json.load(open(rep["file"]))["databases"][0]["db"] == "A"
    assert sent and sent[0][1]["force_all"] is True and "RÉUSSI" in sent[0][0][2]


# ------------------------------------------------------------------ jobs
def base(**kw):
    p = {"db": "LNHC_Prod_2025", "as_name": "COPIE_1", "target": "testvm", "when": None,
         "replace": False, "neutralize": "auto", "with_filestore": True, "confirm": None}
    p.update(kw)
    return p


def test_validate_roles(cfg):
    with pytest.raises(OdooGuardError, match="opérateur"):
        jobs.validate_restore_params(cfg, "lecteur", base())
    assert jobs.validate_restore_params(cfg, "operateur", base())["as_name"] == "COPIE_1"
    with pytest.raises(OdooGuardError, match="admin"):
        jobs.validate_restore_params(cfg, "operateur", base(replace=True, confirm="COPIE_1"))
    with pytest.raises(OdooGuardError, match="retaper"):
        jobs.validate_restore_params(cfg, "admin", base(replace=True, confirm="COPIE"))
    v = jobs.validate_restore_params(cfg, "admin", base(replace=True, confirm="COPIE_1"))
    assert v["replace"]


def test_validate_rules(cfg):
    with pytest.raises(OdooGuardError, match="inconnue"):
        jobs.validate_restore_params(cfg, "admin", base(db="AUTRE"))
    with pytest.raises(OdooGuardError, match="futur"):
        jobs.validate_restore_params(cfg, "admin", base(when="2099-01-01T00:00:00+00:00"))
    with pytest.raises(OdooGuardError, match="même nom en production"):
        jobs.validate_restore_params(cfg, "admin", base(target="odoo", as_name="LNHC_Prod_2025"))
    with pytest.raises(OdooGuardError, match="neutralisée"):
        jobs.validate_restore_params(cfg, "admin", base(neutralize="no"))
    with pytest.raises(OdooGuardError):
        jobs.validate_restore_params(cfg, "admin", base(as_name="x;drop"))


def test_worker_runs_job_with_actor(cfg, monkeypatch):
    seen = {}

    class FakeRestorer:
        def __init__(self, cfg, auditor):
            pass

        def restore_db(self, **kw):
            import os
            seen.update(kw, actor=os.environ.get("ODOOGUARD_ACTOR"))
            return {"control": {"partners": 1}}
    monkeypatch.setattr(jobs, "Restorer", FakeRestorer)
    h = History(cfg.history_db)
    jid = h.job_create("alice", "operateur", "restore-db", base(when="2026-11-22T16:00:00+01:00"))
    bad = h.job_create("bob", "lecteur", "restore-db", base())
    jobs.worker(cfg, h, once=True)
    j = h.job_get(jid)
    assert j["status"] == "success" and seen["actor"] == "alice" and seen["as_name"] == "COPIE_1"
    assert "travail terminé" in open(j["log_path"]).read()
    b = h.job_get(bad)
    assert b["status"] == "failed" and "opérateur" in b["error"]


def test_stale_jobs_failed(cfg):
    h = History(cfg.history_db)
    jid = h.job_create("a", "admin", "restore-test", {})
    h.job_claim()
    assert h.jobs_fail_stale() == 1 and h.job_get(jid)["status"] == "failed"


# ------------------------------------------------------------------ état
def test_status_restore_test_component(cfg, monkeypatch):
    from odooguard import status as status_mod
    from tests.test_history_status import _patch
    cfg.destinations.clear()
    _patch(monkeypatch, wal_lag=2, backup_age=3600, snap_age=600)
    h = History(cfg.history_db)
    comp = lambda: next(c for c in status_mod.compute(cfg, h)["components"] if c["name"] == "restore-test")  # noqa: E731
    assert comp()["level"] == "critical"  # jamais testé
    rid = h.start("restore-test", "hebdo")
    h.finish(rid, "success", message="test ok")
    assert comp()["level"] == "ok"
    rid = h.start("restore-test", "hebdo")
    h.finish(rid, "failed", message="pièces jointes manquantes")
    assert comp()["level"] == "critical"


# ------------------------------------------------------------------ phase 3 bis : manifeste NAS
def test_write_manifest(cfg, tmp_path):
    import hashlib
    import os

    from odooguard import dest as dest_mod
    d = cfg.destinations["nas1"]
    d.path = str(tmp_path / "nas")
    for rel, data in (("pgbackrest/backup/lnhc/backup.info", b"x" * 10),
                      ("restic/data/ab/abcd", b"y" * 100), ("restic/locks/l1", b"z"),
                      ("restic/index/i.tmp", b"t")):
        p = os.path.join(d.path, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(data)
    r = dest_mod.write_manifest(cfg, d, "20261123-010000F", 12)
    assert r == {"file_count": 2, "total_bytes": 110}
    tsv = open(os.path.join(d.path, "MANIFEST.files.tsv"), "rb").read()
    assert tsv == b"pgbackrest/backup/lnhc/backup.info\t10\nrestic/data/ab/abcd\t100\n"
    head = json.load(open(os.path.join(d.path, "MANIFEST.json")))
    assert head["files_sha256"] == hashlib.sha256(tsv).hexdigest()
    assert head["pgbackrest_last_backup"] == "20261123-010000F" and head["file_count"] == 2
