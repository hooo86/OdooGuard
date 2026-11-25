import datetime as dt

import pytest

from odooguard import files
from odooguard.util import OdooGuardError


class FakeRestic(files.Restic):
    def __init__(self, cfg, snaps):
        super().__init__(cfg)
        self._snaps = snaps

    def snapshots(self):
        return self._snaps


SNAPS = [
    {"id": "a" * 64, "short_id": "aaaaaaaa", "time": "2026-11-22T13:50:02.1+01:00"},
    {"id": "b" * 64, "short_id": "bbbbbbbb", "time": "2026-11-22T14:20:03.5+01:00"},
    {"id": "c" * 64, "short_id": "cccccccc", "time": "2026-11-22T14:50:01Z"},
]


def test_snapshot_at_picks_latest_before(cfg):
    r = FakeRestic(cfg, SNAPS)
    t = dt.datetime.fromisoformat("2026-11-22T14:30:00+01:00")
    assert r.snapshot_at(t)["short_id"] == "bbbbbbbb"


def test_snapshot_at_none_before(cfg):
    r = FakeRestic(cfg, SNAPS)
    with pytest.raises(OdooGuardError):
        r.snapshot_at(dt.datetime.fromisoformat("2026-11-01T00:00:00+01:00"))


def test_mirror_path(cfg):
    assert files.mirror_path(cfg, "/etc/odoo").endswith("/mirror/etc/odoo")


def test_rsync_stats():
    out = """
Number of files: 1,234 (reg: 1,000, dir: 234)
Number of regular files transferred: 12
Total file size: 35,123,456 bytes
Total transferred file size: 1,024 bytes
"""
    s = files._rsync_stats(out)
    assert s["number of files"] == 1234
    assert s["number of regular files transferred"] == 12
    assert s["total file size"] == 35123456


def test_pull_mirror_continues_after_missing_source(cfg, monkeypatch):
    """Une source absente (chemin des modules erroné) n'empêche pas les autres."""
    from odooguard import files
    from odooguard.util import CommandError, Result
    cfg.file_sources = ["/var/lib/odoo/filestore", "/opt/odoo/custom-addons", "/etc/odoo"]
    seen = []

    def fake(cmd, cb, **kw):
        src = cmd[-2]
        seen.append(src)
        if "custom-addons" in src:
            raise CommandError(cmd, 23, "rsync: [Receiver] write error: Broken pipe (32)\n"
                                        "Number of files: 0\nTotal file size: 0 bytes")
        return Result(0, "Number of files: 3\nTotal file size: 1,024 bytes\n", "", 0)
    monkeypatch.setattr(files, "run_streaming", fake)
    monkeypatch.setattr(files.Agent, "rsync_source", lambda self, p: "root@h:" + p)
    monkeypatch.setattr(files.Agent, "rsync_ssh", lambda self: "ssh")
    stats = files.pull_mirror(cfg)
    assert len(seen) == 3
    failed = files.failed_sources(stats)
    assert list(failed) == ["/opt/odoo/custom-addons"] and "addons_path" in failed["/opt/odoo/custom-addons"]
    assert stats["/etc/odoo"]["total file size"] == 1024


def test_snapshot_figures_from_history(cfg):
    from odooguard import files
    from odooguard.history import History
    h = History(cfg.history_db)
    rid = h.start("files", "snapshot")
    h.finish(rid, "success", size=60 * 1024**3, added=12 * 1024**2, label="da7ac69a")
    rid = h.start("files", "snapshot")
    h.finish(rid, "failed", size=59 * 1024**3, added=58 * 1024**3, label="4e7f6bff", message="partiel")
    snaps = [{"short_id": "da7ac69a", "time": "2026-11-25T10:14:10.123456789+01:00"},
             {"short_id": "4e7f6bff", "time": "2026-11-25T08:48:34.5Z"},
             {"short_id": "ffffffff", "time": "?", "summary": {"total_bytes_processed": 5, "data_added": 0}}]
    out = files.snapshot_figures(h, snaps)
    assert out[0]["size"] == 60 * 1024**3 and out[0]["added"] == 12 * 1024**2
    assert out[0]["ts"] == 1795598050.0  # 10:14:10+01:00 = 09:14:10 UTC
    assert out[1]["added"] == 58 * 1024**3 and out[1]["ts"] == 1795596514.0
    assert out[2]["size"] == 5 and out[2]["added"] == 0 and out[2]["ts"] is None


def test_restic_time():
    from odooguard.files import restic_time
    assert restic_time("2026-11-25T08:48:34.123456789Z") == restic_time("2026-11-25T09:48:34+01:00")
    assert restic_time("n'importe quoi") is None
