import json
import os
import re
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from odooguard.history import History  # noqa: E402
from odooguard.web import app as app_mod  # noqa: E402
from odooguard.web import auth  # noqa: E402

PW = "motdepasse-solide"


@pytest.fixture
def mk(cfg, tmp_path, monkeypatch):
    cfg.web.users_file = str(tmp_path / "users")
    cfg.web.secret_file = str(tmp_path / "secret")
    h = auth.hash_password(PW, 1000)
    auth.write_accounts(cfg.web.users_file, {"lea": {"role": "lecteur", "hash": h},
                                             "oscar": {"role": "operateur", "hash": h},
                                             "ada": {"role": "admin", "hash": h}})
    existing = {"testvm": set(), "odoo": {"LNHC_Prod_2025"}}

    class FakeAgent:
        def __init__(self, target):
            self.t = target

        def call_json(self, verb, name):
            assert verb == "db-exists"
            return {"exists": name in existing[self.t.name]}
    import odooguard.pg
    import odooguard.remote
    from odooguard.pg import BackupInfo, StanzaInfo
    monkeypatch.setattr(odooguard.remote, "Agent", FakeAgent)
    old = BackupInfo("L", "full", 1_700_000_000, 1_700_000_100, 1, 1, 1, "", "", False)
    monkeypatch.setattr(odooguard.pg.PgBackRest, "info",
                        lambda self, repo_path=None: StanzaInfo(0, "ok", [old], "1", "2", "15", 1))
    monkeypatch.setattr(app_mod, "_noop", None, raising=False)
    hist = History(cfg.history_db)

    def client(user):
        c = TestClient(app_mod.create_app(cfg, hist), base_url="https://testserver")
        r = c.post("/login", data={"username": user, "password": PW}, follow_redirects=False)
        assert r.status_code == 303
        return c
    return client, hist, existing


def csrf_of(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def form(**kw):
    f = {"db": "LNHC_Prod_2025", "moment": "instant", "when": "2026-11-22T16:00:00",
         "target": "testvm", "mode": "copy", "as_name": "", "with_filestore": "on"}
    f.update(kw)
    return f


def test_reader_cannot_restore(mk):
    client, hist, _ = mk
    c = client("lea")
    assert c.get("/restore").status_code == 403
    assert "Restaurer" not in c.get("/").text
    assert c.get("/restore-tests").status_code == 200


def test_csrf_required(mk):
    client, hist, _ = mk
    c = client("oscar")
    r = c.post("/restore/preview", data=form(csrf="faux"))
    assert "Jeton de formulaire invalide" in r.text


def test_copy_flow_creates_job(mk):
    client, hist, _ = mk
    c = client("oscar")
    tok = csrf_of(c.get("/restore").text)
    r = c.post("/restore/preview", data=form(csrf=tok))
    assert r.status_code == 200 and "LNHC_Prod_2025_copie_20261122_1600" in r.text
    assert "neutralisés" in r.text or "oui : courriels" in r.text
    params = re.search(r'name="params" value="([^"]+)"', r.text).group(1).replace("&#34;", '"')
    r = c.post("/restore/submit", data={"csrf": tok, "params": params}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/")
    job = hist.jobs_recent(1)[0]
    p = json.loads(job["params"])
    assert job["user"] == "oscar" and job["status"] == "queued"
    assert p["as_name"] == "LNHC_Prod_2025_copie_20261122_1600" and p["replace"] is False
    page = c.get(r.headers["location"]).text
    assert "en attente" in page
    assert any(a["action"] == "web-restore-request" for a in hist.audit_recent(10))


def test_existing_name_refused_without_replace(mk):
    client, hist, existing = mk
    existing["testvm"].add("DEJA")
    c = client("oscar")
    tok = csrf_of(c.get("/restore").text)
    r = c.post("/restore/preview", data=form(csrf=tok, as_name="DEJA"))
    assert "existe déjà" in r.text


def test_operator_cannot_replace_production(mk):
    client, hist, _ = mk
    c = client("oscar")
    tok = csrf_of(c.get("/restore").text)
    r = c.post("/restore/preview", data=form(csrf=tok, target="odoo", mode="replace"))
    assert "admin" in r.text and not hist.jobs_recent(1)


def test_admin_replace_requires_typed_name(mk):
    client, hist, _ = mk
    c = client("ada")
    tok = csrf_of(c.get("/restore").text)
    r = c.post("/restore/preview", data=form(csrf=tok, target="odoo", mode="replace"))
    assert "PRODUCTION" in r.text and "arrêter Odoo" in r.text and "existe déjà" in r.text
    params = re.search(r'name="params" value="([^"]+)"', r.text).group(1).replace("&#34;", '"')
    bad = c.post("/restore/submit", data={"csrf": tok, "params": params, "confirm": "LNHC"})
    assert "retaper" in bad.text and not hist.jobs_recent(1)
    ok = c.post("/restore/submit", data={"csrf": tok, "params": params, "confirm": "LNHC_Prod_2025"},
                follow_redirects=False)
    assert ok.status_code == 303 and json.loads(hist.jobs_recent(1)[0]["params"])["replace"] is True


def test_tampered_params_revalidated(mk):
    client, hist, _ = mk
    c = client("oscar")
    tok = csrf_of(c.get("/restore").text)
    forged = json.dumps({"db": "LNHC_Prod_2025", "as_name": "LNHC_Prod_2025", "target": "odoo",
                         "when": None, "replace": True, "neutralize": "auto", "with_filestore": True})
    r = c.post("/restore/submit", data={"csrf": tok, "params": forged, "confirm": "LNHC_Prod_2025"})
    assert "admin" in r.text and not hist.jobs_recent(1)


def test_restore_test_button_and_reports(mk, cfg):
    client, hist, _ = mk
    c = client("oscar")
    tok = csrf_of(c.get("/restore-tests").text)
    r = c.post("/restore-tests/run", data={"csrf": tok}, follow_redirects=False)
    assert r.status_code == 303 and hist.jobs_recent(1)[0]["kind"] == "restore-test"
    os.makedirs(os.path.join(cfg.state_dir, "reports"), exist_ok=True)
    rep = {"started_at": time.time() - 600, "ended_at": time.time(), "point": "2026-11-22T16:00:00+01:00",
           "target": "testvm", "level": "warning",
           "databases": [{"db": "LNHC_Prod_2025", "test_db": "ogtest_X", "level": "warning",
                          "checks": [{"name": "Comptages de contrôle", "level": "warning", "detail": "écart"}],
                          "control": {"partners": 10}, "control_production": {"partners": 12},
                          "durations": {"total_s": 500}}]}
    with open(os.path.join(cfg.state_dir, "reports", "restore-test-20261123-063000.json"), "w") as fh:
        json.dump(rep, fh)
    lst = c.get("/restore-tests").text
    assert "/restore-tests/20261123-063000" in lst
    det = c.get("/restore-tests/20261123-063000").text
    assert "ogtest_X" in det and "Comptages de contrôle" in det and ">12<" in det
