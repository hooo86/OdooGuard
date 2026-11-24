"""Gestion des NAS depuis le tableau de bord."""
import json
import os
import re
import subprocess
import sys

import pytest

from odooguard import nas
from odooguard.history import History
from odooguard.util import OdooGuardError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, "server", "odooguard-nas-helper")


def helper(tmp, *args, password=""):
    env = {"PATH": "/usr/bin:/bin", "ODOOGUARD_NAS_HELPER_TESTROOT": str(tmp)}
    r = subprocess.run([sys.executable, "-I", HELPER, *args], input=password + "\n",
                       capture_output=True, text=True, env=env, timeout=30)
    return r.returncode, json.loads(r.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------------ programme root
def test_helper_apply_writes_cred_unit_and_disables_fstab(tmp_path):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/fstab").write_text("//192.168.8.30/odoo /mnt/nas-odoo cifs credentials=x 0 0\nUUID=1 / ext4 defaults 0 1\n")
    rc, out = helper(tmp_path, "apply", "nas1", "//192.168.8.30/odoo", "3.0", "svc", "LNHC", password="s3cr3t")
    assert rc == 0 and out["ok"] and out["fstab_lines_disabled"] == 1
    cred = tmp_path / "etc/odooguard/nas-nas1.cred"
    assert cred.read_text() == "username=svc\npassword=s3cr3t\ndomain=LNHC\n"
    assert oct(cred.stat().st_mode & 0o777) == "0o600"
    unit = (tmp_path / "etc/systemd/system/mnt-odooguard-nas1.mount").read_text()
    assert "What=//192.168.8.30/odoo" in unit and "Where=/mnt/odooguard/nas1" in unit
    assert "credentials=/etc/odooguard/nas-nas1.cred" in unit and ",hard," in unit and "s3cr3t" not in unit
    fstab = (tmp_path / "etc/fstab").read_text()
    assert fstab.startswith("# désactivé par OdooGuard") and "UUID=1 / ext4" in fstab
    assert (tmp_path / "etc/fstab.avant-odooguard-nas").exists()
    # Modification sans mot de passe : l'ancien est conservé
    rc, out = helper(tmp_path, "apply", "nas1", "//192.168.8.30/odoo", "2.1", "svc2", "-")
    assert rc == 0
    assert cred.read_text() == "username=svc2\npassword=s3cr3t\n"
    assert "vers=2.1" in (tmp_path / "etc/systemd/system/mnt-odooguard-nas1.mount").read_text()
    rc, out = helper(tmp_path, "remove", "nas1")
    assert rc == 0 and not cred.exists()
    assert not (tmp_path / "etc/systemd/system/mnt-odooguard-nas1.mount").exists()


@pytest.mark.parametrize("args", [
    ("apply", "NAS1", "//h/s", "3.0", "u", "-"),                       # majuscules
    ("apply", "nas1", "//h/s,uid=0", "3.0", "u", "-"),                 # injection d'options
    ("apply", "nas1", "//h/s", "1.0", "u", "-"),                       # SMB1 refusé
    ("apply", "nas1", "//h/s", "3.0", "u;id", "-"),
    ("apply", "../etc", "//h/s", "3.0", "u", "-"),
    ("remove", "nas1/../../etc"),
    ("format", "nas1"),
])
def test_helper_refuses(tmp_path, args):
    rc, out = helper(tmp_path, *args, password="x")
    assert rc != 0 and not out["ok"]


def test_helper_new_nas_requires_password(tmp_path):
    rc, out = helper(tmp_path, "apply", "nas3", "//h/s", "3.0", "u", "-")
    assert rc != 0 and "mot de passe" in out["error"]


def test_helper_password_newline_refused(tmp_path):
    rc, out = helper(tmp_path, "apply", "nas3", "//h/s", "3.0", "u", "-", password="a\nusername=root")
    assert rc != 0


# ------------------------------------------------------------------ configuration
def test_write_destination_replace_add_remove(tmp_path):
    p = tmp_path / "c.conf"
    p.write_text("# en-tête\n[general]\nsite = X\n\n[destination:nas1]\n# commentaire\npath = /old\n\n[restic]\nkeep_daily = 3\n")
    nas.write_destination(str(p), "nas1", {"path": "/new", "enabled": "yes"})
    s = p.read_text()
    assert "path = /new" in s and "/old" not in s and "# en-tête" in s and "keep_daily = 3" in s
    nas.write_destination(str(p), "nas9", {"path": "/n9"})
    assert "[destination:nas9]" in p.read_text()
    nas.write_destination(str(p), "nas1", None)
    s = p.read_text()
    assert "[destination:nas1]" not in s and "[destination:nas9]" in s and "[restic]" in s
    assert (tmp_path / "c.conf.avant-tableau-de-bord").exists()
    import configparser
    configparser.ConfigParser(interpolation=None).read_string(s)  # toujours valide


def test_validate():
    ok = nas.validate({"name": "nas1", "share": "\\\\192.168.8.30\\odoo", "username": "svc",
                       "smb_version": "3.0", "min_free_gb": "500", "has_password": True},
                      require_password=True)
    assert ok["share"] == "//192.168.8.30/odoo" and ok["subdir"] == "odooguard"
    for bad in ({"name": "Nas 1"}, {"share": "//h/a b"}, {"username": "a b"}, {"subdir": "../x"},
                {"smb_version": "1.0"}, {"has_password": False}):
        p = {"name": "nas1", "share": "//h/s", "username": "u", "smb_version": "3.0", "has_password": True}
        p.update(bad)
        with pytest.raises(OdooGuardError):
            nas.validate(p, require_password=True)


def test_password_stash_is_single_use(cfg):
    tok = nas.stash_password(cfg, "motdepasse")
    path = os.path.join(cfg.state_dir, "pending-secrets", tok)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert nas.take_password(cfg, tok) == "motdepasse"
    assert not os.path.exists(path)
    with pytest.raises(OdooGuardError, match="expiré"):
        nas.take_password(cfg, tok)
    with pytest.raises(OdooGuardError):
        nas.take_password(cfg, "../../etc/passwd")


def test_job_apply_updates_config(cfg, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(nas, "helper", lambda *a, password="", **k: calls.append((a, password)) or {"ok": True})
    monkeypatch.setattr(nas, "MOUNT_BASE", str(tmp_path / "mnt"))
    tok = nas.stash_password(cfg, "pw")
    r = nas.job_apply(cfg, {"name": "nas7", "share": "//10.0.0.7/bk", "username": "svc", "smb_version": "3.0",
                            "subdir": "odooguard", "min_free_gb": 200, "enabled": True, "has_password": True,
                            "secret_token": tok})
    assert calls[0] == (("apply", "nas7", "//10.0.0.7/bk", "3.0", "svc", "-"), "pw")
    assert os.path.isdir(r["path"])
    from odooguard import config as config_mod
    d = config_mod.load(cfg.path).destinations["nas7"]
    assert d.share == "//10.0.0.7/bk" and d.path == r["path"] and d.min_free_gb == 200
    assert "pw" not in open(cfg.path).read()


def test_job_remove_requires_confirmation(cfg, monkeypatch):
    monkeypatch.setattr(nas, "helper", lambda *a, **k: {"ok": True})
    with pytest.raises(OdooGuardError, match="retaper"):
        nas.job_remove(cfg, {"name": "nas1", "confirm": "nas"})
    nas.job_remove(cfg, {"name": "nas1", "confirm": "nas1"})
    from odooguard import config as config_mod
    assert "nas1" not in config_mod.load(cfg.path).destinations


def test_worker_nas_roles(cfg, monkeypatch):
    from odooguard import jobs
    monkeypatch.setattr(nas, "job_apply", lambda c, p: {"message": "monté"})
    monkeypatch.setattr(nas, "refresh_status", lambda *a: None)
    h = History(cfg.history_db)
    bad = h.job_create("oscar", "operateur", "nas-apply", {"name": "nas3"})
    good = h.job_create("ada", "admin", "nas-apply", {"name": "nas3", "secret_token": "abc"})
    jobs.worker(cfg, h, once=True)
    assert h.job_get(bad)["status"] == "failed" and "admin" in h.job_get(bad)["error"]
    assert h.job_get(good)["status"] == "success"
    entries = [json.loads(line) for line in open(cfg.audit_log)]
    assert any(e["action"] == "nas-apply" and "secret_token" not in e for e in entries)


# ------------------------------------------------------------------ web
fastapi = pytest.importorskip("fastapi")


@pytest.fixture
def web(cfg, tmp_path):
    from fastapi.testclient import TestClient

    from odooguard.web import app as app_mod
    from odooguard.web import auth
    cfg.web.users_file = str(tmp_path / "users")
    cfg.web.secret_file = str(tmp_path / "secret")
    h = auth.hash_password("motdepasse-solide", 1000)
    auth.write_accounts(cfg.web.users_file, {u: {"role": r, "hash": h} for u, r in
                                             (("lea", "lecteur"), ("oscar", "operateur"), ("ada", "admin"))})
    hist = History(cfg.history_db)

    def client(user):
        c = TestClient(app_mod.create_app(cfg, hist), base_url="https://testserver")
        c.post("/login", data={"username": user, "password": "motdepasse-solide"})
        return c
    return client, hist


def test_web_nas_pages_and_roles(web):
    client, hist = web
    lea = client("lea")
    page = lea.get("/nas").text
    assert "nas1" in page and "Ajouter un NAS" not in page
    assert lea.get("/nas/new").status_code == 403
    oscar = client("oscar")
    assert "Tester" in oscar.get("/nas").text and oscar.get("/nas/new").status_code == 403


def test_web_nas_save_creates_job_without_password(web, cfg):
    client, hist = web
    ada = client("ada")
    form_html = ada.get("/nas/new").text
    tok = re.search(r'name="csrf" value="([^"]+)"', form_html).group(1)
    assert 'value="nas3"' in form_html
    r = ada.post("/nas/save", data={"csrf": tok, "name": "nas3", "share": "//10.0.0.9/sauvegardes",
                                    "username": "svc", "password": "TresSecret!", "smb_version": "3.0",
                                    "subdir": "odooguard", "min_free_gb": "300", "enabled": "on"},
                 follow_redirects=False)
    assert r.status_code == 303
    job = hist.jobs_recent(1)[0]
    assert job["kind"] == "nas-apply" and "TresSecret" not in job["params"]
    token = json.loads(job["params"])["secret_token"]
    assert open(os.path.join(cfg.state_dir, "pending-secrets", token)).read() == "TresSecret!"
    assert all("TresSecret" not in json.dumps(a) for a in hist.audit_recent(20))


def test_web_nas_save_errors_and_csrf(web):
    client, hist = web
    ada = client("ada")
    tok = re.search(r'name="csrf" value="([^"]+)"', ada.get("/nas/new").text).group(1)
    r = ada.post("/nas/save", data={"csrf": tok, "name": "nas3", "share": "pas un partage",
                                    "username": "svc", "password": "x"})
    assert "partage" in r.text and not hist.jobs_recent(1)
    r = ada.post("/nas/save", data={"csrf": "faux", "name": "nas3", "share": "//h/s", "username": "u"})
    assert "Jeton" in r.text and not hist.jobs_recent(1)
    r = ada.post("/nas/nas1/remove", data={"csrf": tok, "confirm": "nas"})
    assert "annulé" in r.text and not hist.jobs_recent(1)
