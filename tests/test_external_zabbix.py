import json
import time

import pytest

from odooguard import external, zabbix
from odooguard.history import History


def test_validate_report():
    r = external.validate({"status": "SUCCESS", "finished_at": "2026-11-23T02:00:00+01:00",
                           "size_bytes": "1024", "duration_s": 12, "message": "ok"})
    assert r["status"] == "success" and r["size_bytes"] == 1024 and r["finished_at"] > 0
    for bad in ({"status": "done"}, {"status": "success", "size_bytes": "x"},
                {"status": "success", "finished_at": "hier"}, []):
        with pytest.raises(external.ReportError):
            external.validate(bad)


def test_token(cfg, tmp_path):
    ext = cfg.externals["veeam"]
    ext.token_file = str(tmp_path / "tok")
    assert not external.check_token(ext, "abc")  # fichier absent
    (tmp_path / "tok").write_text("abc\n")
    assert external.check_token(ext, "abc")
    assert not external.check_token(ext, "abd")
    assert not external.check_token(ext, "")


def test_external_levels(cfg):
    h = History(cfg.history_db)
    now = time.time()
    lvl = lambda: {c["name"]: c["level"] for c in external.components(cfg, h, now)}  # noqa: E731
    assert lvl()["ext:veeam"] == "critical"  # aucun rapport
    external.store(h, "veeam", {"status": "success", "finished_at": now - 3600}, "1.2.3.4")
    assert lvl()["ext:veeam"] == "ok"
    external.store(h, "veeam", {"status": "warning", "finished_at": now - 60, "message": "1 VM"}, "x")
    assert lvl()["ext:veeam"] == "warning"
    external.store(h, "veeam", {"status": "failed", "finished_at": now, "message": "échec"}, "x")
    assert lvl()["ext:veeam"] == "critical"
    # copie hors ligne : réussie il y a 9 jours > 8 jours tolérés
    external.store(h, "offline", {"status": "success", "finished_at": now - 9 * 86400}, "x")
    assert lvl()["ext:offline"] == "critical"


STATUS = {"generated_at": 1000.0, "overall": "warning", "rpo": {"db_seconds": 2.0, "files_seconds": None},
          "components": [{"name": "db", "level": "ok", "age": 3600.0, "size": 10, "message": "ok"},
                         {"name": "nas:nas1", "level": "critical", "message": "non monté"}]}


def test_zabbix_answers():
    a = lambda *args: zabbix.answer(list(args), STATUS, now=1100)  # noqa: E731
    assert json.loads(a("discovery"))["data"][1]["{#COMPONENT}"] == "nas:nas1"
    assert a("overall") == "1"
    assert a("age") == "100"
    assert a("rpo", "db") == "2"
    assert a("rpo", "files") == "-1"
    assert a("component", "db", "age") == "3600"
    assert a("component", "nas:nas1", "level") == "2"
    assert a("component", "absent", "level") == "3"
    assert a("component", "nas:nas1", "message") == "non monté"


def test_zabbix_stale_never_ok():
    assert zabbix.answer(["overall"], STATUS, now=1000 + 3600) == "3"
    assert zabbix.answer(["overall"], None) == "3"
    assert zabbix.answer(["component", "db", "level"], STATUS, now=99999) == "3"


def test_zabbix_main_reads_file(tmp_path, capsys):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({**STATUS, "generated_at": time.time()}))
    zabbix.main(["--file", str(p), "overall"])
    assert capsys.readouterr().out.strip() == "1"
