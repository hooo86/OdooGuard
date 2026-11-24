"""Planification, notifications et comptes gérés depuis le tableau de bord."""
import json
import os
import re
import subprocess
import sys

import pytest

from odooguard import schedule, settings
from odooguard.history import History
from odooguard.util import OdooGuardError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, "server", "odooguard-schedule-helper")
HAS_SYSTEMD_ANALYZE = os.path.exists("/usr/bin/systemd-analyze")


# ------------------------------------------------------------------ planification
def test_build_specs_modes():
    days = {f"day_{d}": "on" for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat")}
    assert schedule.build_specs({"mode": "times", "times": "01:00, 13:30", **days}) == [
        "Mon,Tue,Wed,Thu,Fri,Sat *-*-* 01:00:00", "Mon,Tue,Wed,Thu,Fri,Sat *-*-* 13:30:00"]
    all_days = {f"day_{d}": "on" for d, _ in schedule.DAYS}
    assert schedule.build_specs({"mode": "times", "times": "1:05", **all_days}) == ["*-*-* 01:05:00"]
    assert schedule.build_specs({"mode": "hourly", "from_hour": "8", "to_hour": "18", "minute": "5", **days}) == [
        "Mon,Tue,Wed,Thu,Fri,Sat *-*-* 08..18:05:00"]
    assert schedule.build_specs({"mode": "interval", "interval": "30min"}) == ["*-*-* *:0/30:00"]
    assert schedule.build_specs({"mode": "interval", "interval": "2h", "minute": "10"}) == ["*-*-* 0/2:10:00"]
    assert schedule.build_specs({"mode": "advanced", "advanced": "Sun *-*-* 01:00:00\n\n"}) == ["Sun *-*-* 01:00:00"]


@pytest.mark.parametrize("p", [
    {"mode": "times", "times": "25:00", "day_Mon": "on"},
    {"mode": "times", "times": "01:00"},                            # aucun jour
    {"mode": "hourly", "from_hour": "18", "to_hour": "8", "day_Mon": "on"},
    {"mode": "interval", "interval": "7min"},
    {"mode": "advanced", "advanced": "Sun 01:00; rm -rf /"},
    {"mode": "inconnu"},
])
def test_build_specs_refuses(p):
    with pytest.raises(OdooGuardError):
        schedule.build_specs(p)


def test_current_reads_unit_and_dropin(tmp_path):
    t = "odooguard-backup-files.timer"
    (tmp_path / t).write_text("[Timer]\nOnCalendar=*-*-* *:20,50:00\nPersistent=true\n")
    cur = schedule.current(t, str(tmp_path))
    assert cur["specs"] == ["*-*-* *:20,50:00"] and not cur["customized"] and not cur["enabled"]
    (tmp_path / f"{t}.d").mkdir()
    (tmp_path / f"{t}.d" / schedule.DROPIN).write_text("[Timer]\nOnCalendar=\nOnCalendar=*-*-* *:0/15:00\n")
    (tmp_path / "timers.target.wants").mkdir()
    (tmp_path / "timers.target.wants" / t).write_text("")
    cur = schedule.current(t, str(tmp_path))
    assert cur["specs"] == ["*-*-* *:0/15:00"] and cur["customized"] and cur["enabled"]
    assert cur["default_specs"] == ["*-*-* *:20,50:00"]


@pytest.mark.skipif(not HAS_SYSTEMD_ANALYZE, reason="systemd-analyze absent")
def test_validate_and_next_run():
    assert schedule.validate_specs(["Mon..Sat *-*-* 08..18:05:00"])
    with pytest.raises(OdooGuardError):
        schedule.validate_specs(["lundi 8h"])
    assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", schedule.next_run(["*-*-* 03:30:00"]))


def helper(tmp, *args):
    env = {"PATH": "/usr/bin:/bin", "ODOOGUARD_SCHEDULE_HELPER_TESTROOT": str(tmp)}
    r = subprocess.run([sys.executable, "-I", HELPER, *args], capture_output=True, text=True, env=env, timeout=30)
    return r.returncode, json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not HAS_SYSTEMD_ANALYZE, reason="systemd-analyze absent")
def test_schedule_helper(tmp_path):
    t = "odooguard-backup-db-incr.timer"
    rc, out = helper(tmp_path, "set", t, "Mon..Fri *-*-* 07..19:05:00", "Sat *-*-* 10:00:00")
    assert rc == 0 and out["ok"]
    dropin = (tmp_path / "etc/systemd/system" / f"{t}.d" / "odooguard-planification.conf").read_text()
    assert "OnCalendar=\nOnCalendar=Mon..Fri *-*-* 07..19:05:00\nOnCalendar=Sat *-*-* 10:00:00\n" in dropin
    rc, out = helper(tmp_path, "reset", t)
    assert rc == 0 and not (tmp_path / "etc/systemd/system" / f"{t}.d" / "odooguard-planification.conf").exists()


@pytest.mark.parametrize("args", [
    ("set", "ssh.service", "*-*-* 01:00:00"),                 # minuteur hors liste
    ("set", "odooguard-status.timer", "*-*-* 01:00:00"),      # le contrôle d'état reste fixe
    ("set", "odooguard-copy.timer", "*-*-* 01:00:00\nExecStart=/bin/sh"),
    ("set", "odooguard-copy.timer", "pas une date"),
    ("set", "odooguard-copy.timer"),
    ("format", "odooguard-copy.timer"),
])
def test_schedule_helper_refuses(tmp_path, args):
    rc, out = helper(tmp_path, *args)
    assert rc != 0 and not out["ok"]


# ------------------------------------------------------------------ notifications
GOOD = {"use_email": True, "use_telegram": True, "min_level": "warning", "repeat_hours": "4",
        "send_resolved": True, "smtp_host": "smtp.office365.com", "smtp_port": "587",
        "smtp_security": "starttls", "smtp_user": "odooguard@lnhc-dz.com", "mail_from": "odooguard@lnhc-dz.com",
        "mail_to": "it@lnhc-dz.com; dsi@lnhc-dz.com", "telegram_chat_ids": "-1001234567890", "https_proxy": ""}


def test_validate_notifications():
    v = settings.validate_notifications(GOOD)
    assert v["channels"] == ["journal", "email", "telegram"] and v["mail_to"] == ["it@lnhc-dz.com", "dsi@lnhc-dz.com"]
    for bad in ({"mail_to": "pas-une-adresse"}, {"smtp_port": "99999"}, {"telegram_chat_ids": "abc"},
                {"smtp_host": ""}, {"min_level": "info"}, {"https_proxy": "ftp://x"}):
        with pytest.raises(OdooGuardError):
            settings.validate_notifications({**GOOD, **bad})


def test_notify_save_writes_config_and_secrets(cfg, tmp_path):
    cfg.alerts.smtp_password_file = str(tmp_path / "smtp.pass")
    cfg.alerts.telegram_token_file = str(tmp_path / "telegram.token")
    p = {**GOOD, "smtp_password_token": settings.stash_secret(cfg, "MotDePasseSMTP"),
         "telegram_token_token": settings.stash_secret(cfg, "123456789:" + "A" * 35)}
    r = settings.job_notify_save(cfg, p)
    assert r["smtp_password_changed"] and r["telegram_token_changed"]
    assert open(cfg.alerts.smtp_password_file).read().strip() == "MotDePasseSMTP"
    assert oct(os.stat(cfg.alerts.smtp_password_file).st_mode & 0o777) == "0o600"
    text = open(cfg.path).read()
    assert "MotDePasseSMTP" not in text and "AAAA" not in text
    from odooguard import config as config_mod
    c = config_mod.load(cfg.path)
    assert c.alerts.channels == ["journal", "email", "telegram"]
    assert c.alerts.mail_to == ["it@lnhc-dz.com", "dsi@lnhc-dz.com"] and c.alerts.telegram_chat_ids == ["-1001234567890"]


def test_notify_save_rejects_bad_token(cfg, tmp_path):
    cfg.alerts.telegram_token_file = str(tmp_path / "t")
    p = {**GOOD, "telegram_token_token": settings.stash_secret(cfg, "pas-un-jeton")}
    with pytest.raises(OdooGuardError, match="jeton Telegram"):
        settings.job_notify_save(cfg, p)


def test_telegram_discover(cfg, monkeypatch, tmp_path):
    class Resp:
        def read(self):
            return json.dumps({"ok": True, "result": [
                {"message": {"chat": {"id": -1001234567890, "title": "Alertes OdooGuard"}}},
                {"message": {"chat": {"id": 42, "first_name": "Hocine"}}}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Opener:
        def open(self, url, timeout):
            assert "/getUpdates" in url
            return Resp()
    monkeypatch.setattr(settings.urllib.request, "build_opener", lambda *h: Opener())
    r = settings.job_telegram_discover(cfg, {"token_token": settings.stash_secret(cfg, "1:x")})
    assert r["chats"] == {"-1001234567890": "Alertes OdooGuard", "42": "Hocine"}


# ------------------------------------------------------------------ comptes
def _accounts(cfg, tmp_path, **acc):
    from odooguard.web import auth
    cfg.web.users_file = str(tmp_path / "users")
    auth.write_accounts(cfg.web.users_file, {n: {"role": r, "hash": "pbkdf2_sha256$1$a$b"} for n, r in acc.items()})
    return auth


def test_user_rules(cfg, tmp_path):
    auth = _accounts(cfg, tmp_path, ada="admin", oscar="operateur")
    with pytest.raises(OdooGuardError, match="dernier administrateur"):
        settings.job_user_save(cfg, {"name": "ada", "role": "lecteur"}, actor="oscar")
    with pytest.raises(OdooGuardError, match="dernier administrateur"):
        settings.job_user_delete(cfg, {"name": "ada", "confirm": "ada"}, actor="oscar")
    with pytest.raises(OdooGuardError, match="propre compte"):
        settings.job_user_delete(cfg, {"name": "oscar", "confirm": "oscar"}, actor="oscar")
    with pytest.raises(OdooGuardError, match="mot de passe obligatoire"):
        settings.job_user_save(cfg, {"name": "nouveau", "role": "lecteur"}, actor="ada")
    h = auth.hash_password("motdepasse-solide", 1000)
    settings.job_user_save(cfg, {"name": "lea", "role": "lecteur",
                                 "hash_token": settings.stash_secret(cfg, h)}, actor="ada")
    settings.job_user_save(cfg, {"name": "oscar", "role": "admin"}, actor="ada")
    acc = auth.read_accounts(cfg.web.users_file)
    assert acc["lea"] == {"role": "lecteur", "hash": h} and acc["oscar"]["role"] == "admin"
    settings.job_user_delete(cfg, {"name": "lea", "confirm": "lea"}, actor="ada")
    assert "lea" not in auth.read_accounts(cfg.web.users_file)


def test_worker_admin_jobs_require_admin(cfg, tmp_path, monkeypatch):
    from odooguard import jobs
    _accounts(cfg, tmp_path, ada="admin")
    h = History(cfg.history_db)
    jid = h.job_create("oscar", "operateur", "user-save", {"name": "x", "role": "admin"})
    jobs.worker(cfg, h, once=True)
    assert h.job_get(jid)["status"] == "failed" and "admin" in h.job_get(jid)["error"]


# ------------------------------------------------------------------ web
fastapi = pytest.importorskip("fastapi")


@pytest.fixture
def web(cfg, tmp_path):
    from fastapi.testclient import TestClient

    from odooguard.web import app as app_mod
    from odooguard.web import auth
    # Emplacements de la configuration elle-même : le service de travaux la relit
    cfg.web.secret_file = str(tmp_path / "secret")
    h = auth.hash_password("motdepasse-solide", 1000)
    auth.write_accounts(cfg.web.users_file, {"lea": {"role": "lecteur", "hash": h},
                                             "ada": {"role": "admin", "hash": h}})
    hist = History(cfg.history_db)

    def client(user):
        c = TestClient(app_mod.create_app(cfg, hist), base_url="https://testserver")
        c.post("/login", data={"username": user, "password": "motdepasse-solide"})
        return c
    return client, hist


def csrf(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_reader_access(web):
    client, hist = web
    lea = client("lea")
    assert lea.get("/reglages").status_code == 200
    assert lea.get("/planification").status_code == 200
    for path in ("/notifications", "/utilisateurs", "/planification/odooguard-copy.timer"):
        assert lea.get(path).status_code == 403, path


@pytest.mark.skipif(not HAS_SYSTEMD_ANALYZE, reason="systemd-analyze absent")
def test_schedule_flow(web):
    client, hist = web
    ada = client("ada")
    tok = csrf(ada.get("/planification").text)
    r = ada.post("/planification/save", data={"csrf": tok, "timer": "odooguard-backup-files.timer",
                                              "mode": "interval", "interval": "30min", "enable": "on"},
                 follow_redirects=False)
    assert r.status_code == 303
    job = hist.jobs_recent(1)[0]
    assert job["kind"] == "schedule" and json.loads(job["params"])["specs"] == ["*-*-* *:0/30:00"]
    r = ada.post("/planification/save", data={"csrf": tok, "timer": "odooguard-backup-files.timer",
                                              "mode": "times", "times": "99:99", "day_Mon": "on"})
    assert "heure invalide" in r.text


def test_notifications_flow_hides_secrets(web, cfg):
    client, hist = web
    ada = client("ada")
    tok = csrf(ada.get("/notifications").text)
    form = {"csrf": tok, **{k: v for k, v in GOOD.items() if not isinstance(v, bool)},
            "use_email": "on", "use_telegram": "on", "send_resolved": "on",
            "smtp_password": "MotDePasseSMTP", "telegram_token": "123456789:" + "B" * 35}
    r = ada.post("/notifications/save", data=form, follow_redirects=False)
    assert r.status_code == 303
    job = hist.jobs_recent(1)[0]
    assert job["kind"] == "notify-save" and "MotDePasseSMTP" not in job["params"] and "BBBB" not in job["params"]
    assert all("MotDePasseSMTP" not in json.dumps(a) for a in hist.audit_recent(20))
    bad = ada.post("/notifications/save", data={**form, "mail_to": "faux"})
    assert "adresse e-mail invalide" in bad.text


def test_users_flow(web, cfg):
    client, hist = web
    ada = client("ada")
    tok = csrf(ada.get("/utilisateurs").text)
    r = ada.post("/utilisateurs/save", data={"csrf": tok, "name": "marie", "role": "operateur",
                                             "password": "court", "password2": "court"})
    assert "trop court" in r.text
    r = ada.post("/utilisateurs/save", data={"csrf": tok, "name": "marie", "role": "operateur",
                                             "password": "motdepasse-marie", "password2": "motdepasse-marie"},
                 follow_redirects=False)
    assert r.status_code == 303
    job = hist.jobs_recent(1)[0]
    assert job["kind"] == "user-save" and "motdepasse-marie" not in job["params"] and "pbkdf2" not in job["params"]
    from odooguard import jobs
    jobs.worker(cfg, hist, once=True)
    assert hist.job_get(job["id"])["status"] == "success"
    from odooguard.web import auth
    assert auth.verify_password("motdepasse-marie", auth.read_accounts(cfg.web.users_file)["marie"]["hash"])
