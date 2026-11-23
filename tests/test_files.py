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
