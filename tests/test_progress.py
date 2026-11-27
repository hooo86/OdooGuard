import json
import time

import pytest

from odooguard import progress
from odooguard.util import run_streaming

# Lignes réelles (rsync 3.2.7 --info=progress2, restic 0.16 --json)
RSYNC = "     10,000,000   5%   44.42MB/s    0:00:00 (xfr#2, to-chk=38/41)"
RESTIC = ('{"message_type":"status","seconds_elapsed":1,"percent_done":0.716134815,"total_files":40,'
          '"files_done":26,"total_bytes":200000000,"bytes_done":143226963,"current_files":["/src/f35"]}')
RESTIC_FIRST = ('{"message_type":"status","seconds_remaining":1,"percent_done":0.2602425,"total_files":40,'
                '"files_done":9,"total_bytes":200000000,"bytes_done":52048500}')


def test_parse_rsync():
    p = progress.parse_rsync_progress(RSYNC)
    assert p["bytes_done"] == 10_000_000 and p["percent"] == 5
    assert p["speed_bps"] == pytest.approx(44.42 * 1024 ** 2)
    assert progress.parse_rsync_progress("sending incremental file list") is None
    assert progress.parse_rsync_progress("Number of files: 41 (reg: 40)") is None


def test_parse_rsync_locales():
    # Système en français (cas réel de la VM OdooGuard) : point des milliers, virgule décimale
    fr = progress.parse_rsync_progress("  8.123.456.789  19%   31,20MB/s    0:04:08  ")
    assert fr["bytes_done"] == 8123456789 and fr["percent"] == 19
    assert abs(fr["speed_bps"] - 31.2 * 1024**2) < 1
    en = progress.parse_rsync_progress("  8,123,456,789  19%   31.20MB/s    0:04:08  ")
    assert en["bytes_done"] == 8123456789
    nbsp = progress.parse_rsync_progress("8\u202f123\u202f456  2%  1,00kB/s  0:00:01")
    assert nbsp["bytes_done"] == 8123456


def test_rsync_runs_with_neutral_locale():
    from odooguard.files import RSYNC_ENV
    assert RSYNC_ENV["LC_ALL"].startswith("C")


def test_parse_restic():
    p = progress.parse_restic_status(RESTIC)
    assert p["percent"] == pytest.approx(71.61, abs=0.01) and p["bytes_total"] == 200_000_000
    assert p["speed_bps"] == 143226963
    first = progress.parse_restic_status(RESTIC_FIRST)
    assert first["speed_bps"] is None and first["eta_s"] == 1
    assert progress.parse_restic_status('{"message_type":"summary"}') is None
    assert progress.parse_restic_status("texte") is None


def test_progress_file_lifecycle(tmp_path):
    with progress.Progress(str(tmp_path), "copy:nas1", "Copie vers nas1") as p:
        p.update(phase="copie", bytes_total=1000, bytes_done=250, force=True)
        items = progress.read_all(str(tmp_path))
        assert len(items) == 1 and items[0]["percent"] == 25 and items[0]["label"] == "Copie vers nas1"
        p.update(phase="autre phase")
        assert progress.read_all(str(tmp_path))[0]["percent"] is None  # remis à zéro
    assert progress.read_all(str(tmp_path)) == []  # fichier supprimé en fin d'opération


def test_progress_speed_and_eta(tmp_path, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(progress.time, "time", lambda: t[0])
    p = progress.Progress(str(tmp_path), "db", "Base")
    p.update(bytes_total=1000, bytes_done=0, force=True)
    t[0] += 10
    p.update(bytes_done=200, force=True)
    assert p.data["speed_bps"] == pytest.approx(20) and p.data["eta_s"] == pytest.approx(40)
    p.close()


def test_progress_eta_from_percent(tmp_path, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(progress.time, "time", lambda: t[0])
    p = progress.Progress(str(tmp_path), "files", "Fichiers")
    p.update(phase="rsync")
    t[0] += 30
    p.update(percent=25, force=True)
    assert p.data["eta_s"] == pytest.approx(90)
    p.close()


def test_stale_progress_flagged(tmp_path):
    d = tmp_path / "progress"
    d.mkdir()
    (d / "db.json").write_text(json.dumps({"label": "x", "started_at": time.time() - 900,
                                           "updated_at": time.time() - 600}))
    assert progress.read_all(str(tmp_path))[0]["stale"] is True


def test_run_streaming_splits_carriage_returns():
    lines = []
    res = run_streaming(["printf", "a 1%%\\rb 2%%\\rc 3%%\\nfin\\n"], lines.append)
    assert lines == ["a 1%", "b 2%", "c 3%", "fin"] and res.returncode == 0


def test_restic_snapshots_without_lock(cfg, monkeypatch):
    from odooguard import files
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        from odooguard.util import Result
        return Result(0, "[]", "", 0)
    monkeypatch.setattr(files, "run", fake_run)
    files.Restic(cfg).snapshots()
    assert "--no-lock" in seen["cmd"]


fastapi = pytest.importorskip("fastapi")


def test_backups_page_shows_progress(cfg, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from odooguard.history import History
    from odooguard.web import app as app_mod
    from odooguard.web import auth
    cfg.web.users_file = str(tmp_path / "users")
    cfg.web.secret_file = str(tmp_path / "secret")
    auth.write_accounts(cfg.web.users_file, {"lea": {"role": "lecteur", "hash": auth.hash_password("motdepasse-solide", 1000)}})
    import odooguard.files
    import odooguard.pg
    monkeypatch.setattr(odooguard.pg.PgBackRest, "info", lambda self, repo_path=None: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(odooguard.files.Restic, "snapshots", lambda self: [])
    p = progress.Progress(cfg.state_dir, "db", "Base PostgreSQL (complète)")
    p.update(phase="copie des fichiers de la base", bytes_total=50 * 1024 ** 3, bytes_done=20 * 1024 ** 3,
             speed_bps=80 * 1024 ** 2, force=True)
    c = TestClient(app_mod.create_app(cfg, History(cfg.history_db)), base_url="https://testserver")
    c.post("/login", data={"username": "lea", "password": "motdepasse-solide"})
    html = c.get("/backups").text
    assert "Sauvegardes en cours" in html and "40.0 %" in html and "80.0 Mio/s" in html
    assert "20.0 Gio / 50.0 Gio" in html
    assert 'hx-get="/partials/progress"' in html
    p.close()
    assert "Sauvegardes en cours" not in c.get("/partials/progress").text


def test_rsync_files_progress_for_incremental_copy(tmp_path):
    # Copie incrémentale : 0 % des octets du dépôt, mais 75 % des fichiers déjà examinés
    line = "    493.700.000   0%  344,10kB/s    0:00:01 (xfr#4200, to-chk=5000/20000)"
    st = progress.parse_rsync_progress(line)
    assert st["files_done"] == 15000 and st["files_total"] == 20000
    p = progress.Progress(str(tmp_path), "copy:nas1", "Copie vers nas1")
    with p:
        p.update(**st, force=True)
        data = json.load(open(p.path))
    assert data["percent"] == 75.0 and data["files_total"] == 20000
