import time

from odooguard import status as status_mod
from odooguard.history import History
from odooguard.pg import BackupInfo, StanzaInfo


def test_history_lifecycle(tmp_path):
    h = History(str(tmp_path / "h.db"))
    rid = h.start("db", "incr")
    assert h.last("db")["status"] == "running"
    h.finish(rid, "failed", message="NAS absent", category="mount")
    last = h.last("db")
    assert last["status"] == "failed" and last["category"] == "mount"
    assert h.last("db", status="success") is None


def test_interrupted_runs_marked_failed(tmp_path):
    h = History(str(tmp_path / "h.db"))
    rid = h.start("files", "snapshot")
    with h._conn() as c:
        c.execute("UPDATE runs SET started_at=? WHERE id=?", (time.time() - 10 * 3600, rid))
    assert h.mark_interrupted() == 1
    assert h.last("files")["category"] == "interrupted"


def test_rpo_report(tmp_path):
    h = History(str(tmp_path / "h.db"))
    for v in (3, 12, 7):
        h.metric("rpo_db_seconds", v)
    rep = status_mod.rpo_report(h, 7)
    assert rep["db_seconds"] == {"max": 12, "samples": 3}


def _patch(monkeypatch, *, wal_lag, backup_age, snap_age):
    now = time.time()
    monkeypatch.setattr(status_mod, "wal_state", lambda cfg, query_source=True: {
        "ok": wal_lag < 600, "lag_seconds": wal_lag, "pending": 0, "slot_wal_status": "reserved"})
    b = BackupInfo("L", "full", int(now - backup_age - 60), int(now - backup_age), 10, 5, 5,
                   "", "", False)
    monkeypatch.setattr(status_mod.PgBackRest, "info",
                        lambda self, repo_path=None: StanzaInfo(0, "ok", [b], "1", "2", "15", 1))
    import datetime as dt
    t = dt.datetime.fromtimestamp(now - snap_age, dt.timezone.utc).isoformat()
    monkeypatch.setattr(status_mod.Restic, "snapshots",
                        lambda self: [{"short_id": "abc", "time": t, "summary": {}}])


def test_status_ok(cfg, monkeypatch):
    cfg.destinations.clear()
    _patch(monkeypatch, wal_lag=2, backup_age=3600, snap_age=600)
    st = status_mod.compute(cfg, History(cfg.history_db))
    levels = {c["name"]: c["level"] for c in st["components"]}
    assert levels["wal"] == "ok" and levels["db"] == "ok" and levels["files"] == "ok"
    assert st["rpo"]["db_seconds"] == 2


def test_status_wal_late_is_critical(cfg, monkeypatch):
    cfg.destinations.clear()
    _patch(monkeypatch, wal_lag=1200, backup_age=3600, snap_age=600)
    st = status_mod.compute(cfg, History(cfg.history_db))
    assert st["overall"] == "critical"


def test_status_nas_unmounted_is_critical(cfg, monkeypatch):
    _patch(monkeypatch, wal_lag=2, backup_age=3600, snap_age=600)
    st = status_mod.compute(cfg, History(cfg.history_db))
    nas = [c for c in st["components"] if c["name"].startswith("nas:")]
    assert nas and all(c["level"] == "critical" for c in nas)
    assert all(c["category"] == "mount" for c in nas)


def test_status_failed_copy_io(cfg, monkeypatch):
    for d in cfg.destinations.values():
        d.mountpoint = ""
        import os
        os.makedirs(d.path, exist_ok=True)
        d.min_free_gb = 0
    _patch(monkeypatch, wal_lag=2, backup_age=3600, snap_age=600)
    h = History(cfg.history_db)
    rid = h.start("copy:nas1", "copy")
    h.finish(rid, "failed", category="io", message="Input/output error")
    st = status_mod.compute(cfg, h)
    copy = next(c for c in st["components"] if c["name"] == "copy:nas1")
    assert copy["level"] == "critical"
