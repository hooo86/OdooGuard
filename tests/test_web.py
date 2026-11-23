import json
import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from odooguard.history import History  # noqa: E402
from odooguard.web import auth  # noqa: E402
from odooguard.web.app import create_app  # noqa: E402


@pytest.fixture
def client(cfg, tmp_path):
    cfg.web.users_file = str(tmp_path / "users")
    cfg.web.secret_file = str(tmp_path / "secret")
    auth.write_users(cfg.web.users_file, {"admin": auth.hash_password("motdepasse-solide", 1000)})
    st = {"generated_at": time.time(), "overall": "critical", "site": "LNHC",
          "rpo": {"db_seconds": 4, "files_seconds": 1200},
          "components": [
              {"name": "wal", "level": "ok", "age": 4, "message": "retard WAL 4 s"},
              {"name": "db", "level": "ok", "age": 3600, "size": 50 * 1024**3, "added": 2 * 1024**3,
               "duration": 600, "message": "ok"},
              {"name": "nas:nas1", "level": "critical", "message": "inaccessible : <script>x</script>"},
              {"name": "ext:veeam", "label": "Veeam B&R", "level": "warning", "age": 7200,
               "message": "1 VM en avertissement"}]}
    with open(cfg.status_file, "w") as fh:
        json.dump(st, fh)
    h = History(cfg.history_db)
    for i in range(10):
        h.metric("rpo_db_seconds", 2 + i, time.time() - (10 - i) * 300)
    rid = h.start("db", "incr")
    h.finish(rid, "success", size=100, added=10)
    app = create_app(cfg, h)
    return TestClient(app, base_url="https://testserver")


def login(c, pw="motdepasse-solide"):
    return c.post("/login", data={"username": "admin", "password": pw}, follow_redirects=False)


def test_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/api/v1/status").status_code == 401
    r = client.get("/partials/status", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"


def test_login_and_dashboard(client):
    r = login(client)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=strict" in cookie.lower()
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Critique" in html and "Veeam B&amp;R" in html and "50.0 Gio" in html
    assert "<script>x</script>" not in html  # échappement
    assert "linechart" in html
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert client.get("/api/v1/status").json()["overall"] == "critical"
    for path in ("/history", "/alerts", "/audit", "/partials/status"):
        assert client.get(path).status_code == 200, path


def test_bad_password_and_throttle(client):
    for _ in range(5):
        r = login(client, "faux")
        assert "incorrect" in r.text
    r = login(client)  # même le bon mot de passe est bloqué
    assert "Trop de tentatives" in r.text


def test_stale_status_banner(client, cfg):
    st = json.load(open(cfg.status_file))
    st["generated_at"] = time.time() - 3600
    json.dump(st, open(cfg.status_file, "w"))
    login(client)
    html = client.get("/").text
    assert "État périmé" in html and "Inconnu" in html


def test_external_report_api(client, cfg, tmp_path):
    tok = tmp_path / "veeam"
    tok.write_text("jeton-veeam")
    cfg.externals["veeam"].token_file = str(tok)
    url = "/api/v1/report/veeam"
    body = {"status": "success", "finished_at": "2026-11-23T02:00:00+01:00", "message": "5 VM"}
    assert client.post(url, json=body).status_code == 403
    assert client.post(url, json=body, headers={"Authorization": "Bearer mauvais"}).status_code == 403
    assert client.post("/api/v1/report/inconnu", json=body,
                       headers={"Authorization": "Bearer jeton-veeam"}).status_code == 403
    r = client.post(url, json={"status": "bof"}, headers={"Authorization": "Bearer jeton-veeam"})
    assert r.status_code == 400
    r = client.post(url, json=body, headers={"Authorization": "Bearer jeton-veeam"})
    assert r.status_code == 200 and r.json()["stored"]
    assert History(cfg.history_db).external_last("veeam")["message"] == "5 VM"


def test_session_tampering(cfg):
    secret = b"x" * 64
    tok = auth.make_session(secret, "admin", 1)
    assert auth.read_session(secret, tok) == "admin"
    assert auth.read_session(b"y" * 64, tok) is None
    assert auth.read_session(secret, tok[:-2] + "AA") is None
    assert auth.read_session(secret, auth.make_session(secret, "admin", -1)) is None


def test_password_hash():
    h = auth.hash_password("abc", 1000)
    assert auth.verify_password("abc", h) and not auth.verify_password("abd", h)
    assert not auth.verify_password("abc", "garbage")


def test_healthz_no_auth(client):
    assert client.get("/healthz").json()["web"] == "ok"
