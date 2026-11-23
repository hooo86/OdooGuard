import os
import time

import pytest

from odooguard import pg
from odooguard.util import OdooGuardError


def test_parse_info(fixture_path):
    raw = open(fixture_path("pgbackrest_info.json")).read()
    info = pg.parse_info(raw, "lnhc")
    assert info.status_code == 0
    assert [b.type for b in info.backups] == ["full", "incr"]
    assert info.last().type == "incr"
    assert info.last(("full",)).label == "20261123-121703F"
    assert info.wal_min == "000000010000000000000001"
    assert info.wal_max >= info.backups[-1].wal_stop
    assert info.backups[0].duration >= 0


def test_parse_info_unknown_stanza(fixture_path):
    with pytest.raises(OdooGuardError):
        pg.parse_info(open(fixture_path("pgbackrest_info.json")).read(), "autre")


class FakePgbr:
    def __init__(self, fail_on=None):
        self.pushed = []
        self.fail_on = fail_on

    def exec(self, *args, config=None, timeout=None, check=True):
        assert args[0] == "archive-push"
        name = os.path.basename(args[1])
        if name == self.fail_on:
            from odooguard.util import CommandError
            raise CommandError(["pgbackrest"], 1, "boom")
        self.pushed.append(name)


def _touch(d, name, data=b"x"):
    with open(os.path.join(d, name), "wb") as fh:
        fh.write(data)


def test_ingest_order_and_markers(cfg):
    sp = cfg.wal_spool
    for n in ("000000010000000000000003", "000000010000000000000002",
              "000000010000000000000004.partial", "00000002.history", "garbage.tmp"):
        _touch(sp, n)
    fake = FakePgbr()
    ing = pg.WalIngest(cfg, fake)
    assert ing.pending() == ["00000002.history", "000000010000000000000002",
                             "000000010000000000000003"]
    assert ing.run_once() == 3
    assert fake.pushed[0] == "00000002.history"
    assert ing.pending() == []  # marqués comme versés, .partial ignoré
    assert ing.run_once() == 0


def test_ingest_failure_stops_in_order(cfg):
    for n in ("000000010000000000000001", "000000010000000000000002"):
        _touch(cfg.wal_spool, n)
    ing = pg.WalIngest(cfg, FakePgbr(fail_on="000000010000000000000001"))
    with pytest.raises(OdooGuardError):
        ing.run_once()
    # Rien n'est marqué : le segment sera retenté au prochain passage
    assert len(ing.pending()) == 2


def test_cleanup_keeps_recent(cfg):
    cfg.wal_keep_local = 2
    names = [f"0000000100000000000000{i:02X}" for i in range(1, 6)]
    for n in names:
        _touch(cfg.wal_spool, n)
    ing = pg.WalIngest(cfg, FakePgbr())
    ing.run_once()
    remaining = sorted(n for n in os.listdir(cfg.wal_spool) if pg.WAL_RE.match(n))
    assert remaining == names[-2:]


def test_spool_state(cfg):
    _touch(cfg.wal_spool, "000000010000000000000009.partial")
    st = pg.spool_state(cfg)
    assert st["partial"] == "000000010000000000000009.partial"
    assert time.time() - st["last_write"] < 5


def test_wal_state_offline_uses_spool(cfg):
    _touch(cfg.wal_spool, "000000010000000000000009.partial")
    st = pg.wal_state(cfg, query_source=False)
    assert st["source"] == "spool"
    assert st["ok"] is True
    old = time.time() - 3600
    os.utime(os.path.join(cfg.wal_spool, "000000010000000000000009.partial"), (old, old))
    st = pg.wal_state(cfg, query_source=False)
    assert st["ok"] is False and st["lag_seconds"] > 3000
