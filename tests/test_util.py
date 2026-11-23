import datetime as dt
import os
import threading

import pytest

from odooguard import util


REF = dt.datetime(2026, 11, 23, 10, 0, tzinfo=dt.timezone.utc).astimezone()


@pytest.mark.parametrize("value,expected", [
    ("-2h", REF - dt.timedelta(hours=2)),
    ("-45min", REF - dt.timedelta(minutes=45)),
    ("-1j", REF - dt.timedelta(days=1)),
])
def test_parse_relative(value, expected):
    assert util.parse_time(value, reference=REF) == expected


def test_parse_yesterday():
    t = util.parse_time("hier 14:30", reference=REF)
    assert t.date() == REF.date() - dt.timedelta(days=1)
    assert (t.hour, t.minute) == (14, 30)


def test_parse_iso_and_space():
    t = util.parse_time("2026-11-22 14:30:15")
    assert (t.year, t.month, t.day, t.hour, t.minute, t.second) == (2026, 11, 22, 14, 30, 15)
    assert t.tzinfo is not None
    t2 = util.parse_time("2026-11-22T14:30:15+01:00")
    assert t2.utcoffset() == dt.timedelta(hours=1)


def test_parse_invalid():
    with pytest.raises(util.OdooGuardError):
        util.parse_time("demain matin")


def test_pg_time_has_timezone():
    s = util.pg_time(REF)
    assert s.startswith("2026-11-23 ") and (s[-5] in "+-")


def test_fmt():
    assert util.fmt_age(None) == "jamais"
    assert util.fmt_age(30) == "30 s"
    assert util.fmt_age(3600) == "60 min"
    assert util.fmt_size(1024 ** 3) == "1.0 Gio"


def test_error_classification():
    e = util.CommandError(["rsync"], 11, "rsync: write failed: Input/output error (5)")
    assert e.category == "io"
    e = util.CommandError(["x"], 1, "No space left on device")
    assert e.category == "space"
    e = util.CommandError(["x"], 1, "permission denied")
    assert e.category == "command"


def test_io_probe_ok(tmp_path):
    assert util.io_probe(str(tmp_path)) >= 0
    assert os.listdir(tmp_path) == []  # aucun résidu


def test_io_probe_readonly(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    if os.geteuid() == 0:
        pytest.skip("root contourne les droits")
    with pytest.raises(util.OdooGuardError) as exc:
        util.io_probe(str(ro))
    assert exc.value.category == "io"


def test_io_probe_missing_dir(tmp_path):
    with pytest.raises(util.OdooGuardError):
        util.io_probe(str(tmp_path / "absent"))


def test_check_free_space(tmp_path):
    util.check_free_space(str(tmp_path), 0, "test")
    with pytest.raises(util.OdooGuardError) as exc:
        util.check_free_space(str(tmp_path), 10 ** 9, "test")
    assert exc.value.category == "space"


def test_file_lock_exclusive(tmp_path):
    with util.file_lock(str(tmp_path), "db"):
        errors = []

        def other():
            try:
                with util.file_lock(str(tmp_path), "db"):
                    pass
            except util.OdooGuardError as exc:
                errors.append(exc)
        t = threading.Thread(target=other)
        t.start()
        t.join()
        # flock est par descripteur : un 2e open dans le même processus est refusé
        assert errors and errors[0].category == "lock"
    with util.file_lock(str(tmp_path), "db"):
        pass  # libéré


def test_run_no_shell():
    res = util.run(["echo", "$(id)"])
    assert res.stdout.strip() == "$(id)"


def test_run_failure():
    with pytest.raises(util.CommandError):
        util.run(["false"])
    assert util.run(["false"], check=False).returncode == 1


def test_business_hours():
    monday_10 = dt.datetime(2026, 11, 23, 10, 0).astimezone()
    sunday_10 = dt.datetime(2026, 11, 22, 10, 0).astimezone()
    monday_20 = dt.datetime(2026, 11, 23, 20, 0).astimezone()
    assert util.in_business_hours(monday_10)
    assert not util.in_business_hours(sunday_10)
    assert not util.in_business_hours(monday_20)
