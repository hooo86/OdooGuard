import json
import re

import pytest

from odooguard import jobs
from odooguard.history import History
from odooguard.util import OdooGuardError, Result


def fake_run_factory(calls, active=""):
    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["systemctl", "is-active"]:
            return Result(3 if not active else 0, active or "inactive", "", 0)
        return Result(0, "", "", 0)
    return fake_run


def test_start_backup_exact_command(monkeypatch):
    calls = []
    import odooguard.util
    monkeypatch.setattr(odooguard.util, "run", fake_run_factory(calls))
    r = jobs.start_backup("db-full")
    assert calls[-1] == ["sudo", "-n", "/usr/bin/systemctl", "start", "--no-block",
                         "odooguard-backup-db@full.service"]
    assert "complète" in r["message"]


def test_start_backup_refuses_unknown_and_running(monkeypatch):
    import odooguard.util
    with pytest.raises(OdooGuardError, match="inconnue"):
        jobs.start_backup("db-full; reboot")
    monkeypatch.setattr(odooguard.util, "run", fake_run_factory([], active="active"))
    with pytest.raises(OdooGuardError, match="déjà en cours"):
        jobs.start_backup("files")


def test_sudoers_lines_match_units():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "deploy", "sudoers", "odooguard")).read()
    for unit, _ in jobs.BACKUP_UNITS.values():
        assert f"/usr/bin/systemctl start --no-block {unit}" in text


def test_worker_backup_start_roles(cfg, monkeypatch):
    monkeypatch.setattr(jobs, "start_backup", lambda what: {"unit": "u.service", "message": "lancée"})
    h = History(cfg.history_db)
    bad = h.job_create("lea", "lecteur", "backup-start", {"what": "files"})
    good = h.job_create("oscar", "operateur", "backup-start", {"what": "files"})
    jobs.worker(cfg, h, once=True)
    assert h.job_get(bad)["status"] == "failed"
    assert h.job_get(good)["status"] == "success"


fastapi = pytest.importorskip("fastapi")


@pytest.fixture
def web(cfg, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import odooguard.files
    import odooguard.pg
    from odooguard.web import app as app_mod
    from odooguard.web import auth
    cfg.web.users_file = str(tmp_path / "users")
    cfg.web.secret_file = str(tmp_path / "secret")
    h = auth.hash_password("motdepasse-solide", 1000)
    auth.write_accounts(cfg.web.users_file, {"lea": {"role": "lecteur", "hash": h},
                                             "oscar": {"role": "operateur", "hash": h}})
    monkeypatch.setattr(odooguard.pg.PgBackRest, "info", lambda self, repo_path=None: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(odooguard.files.Restic, "snapshots", lambda self: [])
    hist = History(cfg.history_db)

    def client(user):
        c = TestClient(app_mod.create_app(cfg, hist), base_url="https://testserver")
        c.post("/login", data={"username": user, "password": "motdepasse-solide"})
        return c
    return client, hist


def test_web_buttons_and_request(web):
    client, hist = web
    assert "Lancer une sauvegarde maintenant" not in client("lea").get("/backups").text
    oscar = client("oscar")
    html = oscar.get("/backups").text
    assert "Lancer une sauvegarde maintenant" in html and "Base : sauvegarde complète" in html
    tok = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    r = oscar.post("/backups/start", data={"csrf": tok, "what": "files"}, follow_redirects=False)
    assert r.status_code == 303 and "lancee=files" in r.headers["location"]
    job = hist.jobs_recent(1)[0]
    assert job["kind"] == "backup-start" and json.loads(job["params"]) == {"what": "files"}
    page = oscar.get(r.headers["location"]).text
    assert "Demande enregistrée" in page and "fichiers (filestore" in page
    assert "inconnue" in oscar.post("/backups/start", data={"csrf": tok, "what": "rm"}).text
    assert "Jeton" in oscar.post("/backups/start", data={"csrf": "x", "what": "files"}).text


def test_web_reader_cannot_start(web):
    client, hist = web
    lea = client("lea")
    r = lea.post("/backups/start", data={"csrf": "x", "what": "files"})
    assert r.status_code == 403 and not hist.jobs_recent(1)
