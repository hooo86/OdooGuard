

def test_mark_interrupted_dead_pid_and_legacy(tmp_path, monkeypatch):
    import time
    from odooguard import history
    monkeypatch.setattr(history, "_pid_alive", lambda pid: pid == 4242)
    h = history.History(str(tmp_path / "h.db"))
    now = time.time()
    with h._conn() as c:
        ins = ("INSERT INTO runs(component, kind, started_at, status, pid) VALUES (?,?,?,?,?)")
        c.execute(ins, ("copy:nas1", "copy", now - 9 * 3600, "running", 4242))  # longue mais vivante
        c.execute(ins, ("db", "incr", now, "running", 999))                     # processus disparu
        c.execute(ins, ("files", "snapshot", now - 600, "running", None))       # ancienne ligne, remplacée
        c.execute(ins, ("files", "snapshot", now - 60, "running", None))        # ancienne ligne, la dernière
    assert h.mark_interrupted() == 2
    st = {(r["component"], round(now - r["started_at"])): (r["status"], r["category"]) for r in h.recent(10)}
    assert st[("copy:nas1", 9 * 3600)] == ("running", None)
    assert st[("db", 0)] == ("failed", "interrupted")
    assert st[("files", 600)] == ("failed", "interrupted")
    assert st[("files", 60)] == ("running", None)
    assert h.start("db", "full") and h.recent(1)[0]["pid"]
