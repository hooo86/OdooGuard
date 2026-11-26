"""Surveillance : stockage, services, sécurité Odoo, fuseau horaire, test par canal."""
import json
import os
import re
import subprocess
import sys
import time

import pytest

from odooguard import monitor, schedule, settings
from odooguard.history import History
from odooguard.util import OdooGuardError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(ROOT, "server", "odooguard-agent")


def extract_agent_python():
    """Script Python embarqué dans le verbe « monitor » de l'agent."""
    text = open(AGENT, encoding="utf-8").read()
    start = text.index("<<'PY'\n", text.index("monitor)")) + len("<<'PY'\n")
    return text[start:text.index("\nPY\n", start)]


# ------------------------------------------------------------------ agent
def test_agent_monitor_reads_odoo_log_and_rotation(tmp_path):
    script = tmp_path / "m.py"
    script.write_text(extract_agent_python())
    log = tmp_path / "odoo.log"
    log.write_text(
        "2026-11-25 07:00:01,123 42 INFO P odoo.addons.base.models.res_users: Login successful for db:P login:admin from 192.168.8.10\n"
        "2026-11-25 07:01:02,123 42 INFO P odoo.addons.base.models.res_users: Login failed for db:P login:root from 10.0.0.5\n"
        "2026-11-25 07:01:03,123 42 INFO ? werkzeug: 10.0.0.5 - - \"POST /web/login HTTP/1.1\" 200 -\n")

    def run(cursor):
        r = subprocess.run([sys.executable, "-I", str(script), cursor, str(log), "odoo-inexistant",
                            str(tmp_path), str(tmp_path), "", json.dumps({"P": []})],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    out = run("-")
    ev = out["auth"]["events"]
    assert [(e["ok"], e["login"], e["ip"]) for e in ev] == [(True, "admin", "192.168.8.10"), (False, "root", "10.0.0.5")]
    assert ev[1]["ts"] == 1795590062.0  # heure du journal d'Odoo lue en UTC
    assert out["disks"] and out["users"] == {"P": []}
    cur = out["auth"]["cursor"]
    assert run(cur)["auth"]["events"] == []          # rien de nouveau
    # rotation : la fin de l'ancien fichier puis le nouveau
    with open(log, "a") as fh:
        fh.write("2026-11-25 07:05:00,1 1 INFO P x: Login failed for db:P login:bob from 1.2.3.4\n")
    os.rename(log, str(log) + ".1")
    log.write_text("2026-11-25 07:06:00,1 1 INFO P x: Login failed for db:P login:eve from 1.2.3.4\n")
    assert [e["login"] for e in run(cur)["auth"]["events"]] == ["bob", "eve"]
    # Comptes passés par fichier (des milliers de comptes dépassent la limite d'un argument)
    big = {"P": [{"id": i, "login": f"user{i}@exemple.dz", "name": "x" * 40} for i in range(5000)]}
    f = tmp_path / "users.json"
    f.write_text(json.dumps(big))
    r = subprocess.run([sys.executable, "-I", str(script), "-", str(log), "odoo-inexistant", str(tmp_path),
                        str(tmp_path), "", str(f)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert len(json.loads(r.stdout)["users"]["P"]) == 5000


def test_agent_monitor_verb_validates_cursor():
    text = open(AGENT, encoding="utf-8").read()
    assert 'monitor)' in text and "curseur invalide" in text
    rx = re.search(r'\[\[ "\$cursor" =~ (\S+) \]\]', text).group(1)
    ok = re.compile(rx.replace("{1,20}", "{1,20}"))
    for c in ("-", "f:123:456", "j:1795590062"):
        assert ok.match(c), c
    for c in ("f:1:2;rm", "j:abc", "x"):
        assert not ok.match(c), c


# ------------------------------------------------------------------ comptes Odoo
def test_diff_users():
    before = {"1": {"login": "admin", "active": True, "name": "Admin"},
              "5": {"login": "ali", "active": True, "name": "Ali"},
              "7": {"login": "old", "active": True, "name": "Ancien"}}
    users = [{"id": 1, "login": "admin", "active": True},
             {"id": 5, "login": "ali", "active": False, "written": 100, "written_by": "admin", "name": "Ali"},
             {"id": 9, "login": "new", "active": True, "created": 50, "created_by": "admin", "name": "Nouveau"}]
    ev = monitor.diff_users("P", before, users, now=1000)
    kinds = {(e["kind"], e["login"]) for e in ev}
    assert kinds == {("user_archived", "ali"), ("user_created", "new"), ("user_deleted", "old")}
    assert monitor.diff_users("P", None, users, now=1000) == []   # premier relevé : aucune alerte


def test_collect_with_agent(cfg, monkeypatch):
    hist = History(cfg.history_db)
    calls = []

    class FakeAgent:
        def __init__(self, target):
            pass

        def call_json(self, verb, *args, timeout=None):
            calls.append((verb, args))
            users = [{"id": 2, "login": "admin", "active": True}]
            if len(calls) > 1:
                users.append({"id": 3, "login": "compta", "active": True, "created": time.time(),
                              "created_by": "admin", "name": "Compta"})
            return {"version": 1,
                    "disks": [{"label": "système", "mount": "/", "total": 100, "free": 5, "used": 95}],
                    "services": [{"unit": "odoo.service", "active": "failed", "sub": "failed", "load": "loaded"},
                                 {"unit": "postgresql@15-main.service", "active": "active", "sub": "running"}],
                    "failed_units": ["odoo.service"],
                    "auth": {"cursor": f"f:1:{len(calls)}", "source": "fichier",
                             "events": [{"ts": time.time(), "ok": False, "db": "P", "login": f"x{i}", "ip": "6.6.6.6"}
                                        for i in range(cfg.monitoring.failed_logins_warn)]},
                    "users": {db: users for db in cfg.databases}}
    monkeypatch.setattr("odooguard.remote.Agent", FakeAgent)
    sent = []
    monkeypatch.setattr("odooguard.notify.dispatch", lambda *a, **k: sent.append((a, k)) or {})
    comps, data = monitor.collect(cfg, hist)
    by = {c["name"]: c for c in comps}
    assert calls[0][0] == "monitor" and calls[0][1][0] == "-"
    assert by["stockage:odoo:/"]["level"] == "critical"
    assert by["service:odoo:odoo.service"]["level"] == "critical"
    assert by["service:odoo:postgresql@15-main.service"]["level"] == "ok"
    assert by["securite:odoo"]["level"] == "warning" and "6.6.6.6" in by["securite:odoo"]["message"]
    assert all(c["group"] == "monitor" for c in comps)
    assert hist.kv_get("odoo_auth_cursor") == "f:1:1"
    assert not sent                                   # premier relevé des comptes : pas d'alerte
    comps, data = monitor.collect(cfg, hist)
    assert calls[1][1][0] == "f:1:1"                  # curseur repris
    assert sent and "compta" in sent[0][0][3]         # compte créé => notification
    assert hist.security_events(0, ("user_created",))[0]["login"] == "compta"
    assert data["security"]["failed_24h"] == 2 * cfg.monitoring.failed_logins_warn


def test_collect_agent_too_old(cfg, monkeypatch):
    from odooguard.util import CommandError

    class OldAgent:
        def __init__(self, target):
            pass

        def call_json(self, *a, **k):
            raise CommandError(["ssh"], 1, "odooguard-agent: verbe inconnu : monitor")
    monkeypatch.setattr("odooguard.remote.Agent", OldAgent)
    comps, data = monitor.collect(cfg, History(cfg.history_db))
    c = next(c for c in comps if c["name"] == "surveillance:odoo")
    assert c["level"] == "warning" and "mettre à jour" in c["message"]
    assert any(d["host"] == "odooguard" for d in data["storage"])


def test_disk_levels(cfg):
    assert monitor.disk_level(cfg, 50) == "ok"
    assert monitor.disk_level(cfg, cfg.monitoring.disk_warn_pct) == "warning"
    assert monitor.disk_level(cfg, 99) == "critical"


def test_validate_monitoring():
    base = {"disk_warn_pct": "80", "disk_crit_pct": "90", "services_local": "odooguard-web, odooguard-jobs.service",
            "failed_logins_warn": "10", "failed_logins_window": "15", "keep_days": "180", "security": True}
    v = settings.validate_monitoring(base)
    assert v["services_local"] == ["odooguard-web.service", "odooguard-jobs.service"]
    for bad in ({"disk_warn_pct": "95"}, {"services_local": "a;rm -rf /"}, {"failed_logins_warn": "0"}):
        with pytest.raises(OdooGuardError):
            settings.validate_monitoring({**base, **bad})


def test_monitoring_save_roundtrip(cfg):
    from odooguard import config as config_mod
    settings.job_monitoring_save(cfg, {"disk_warn_pct": "70", "disk_crit_pct": "85",
                                       "services_local": "odooguard-web", "failed_logins_warn": "5",
                                       "failed_logins_window": "10", "keep_days": "30",
                                       "security": True, "notify_accounts": False})
    m = config_mod.load(cfg.path).monitoring
    assert (m.disk_warn_pct, m.disk_crit_pct, m.failed_logins_warn, m.notify_accounts) == (70, 85, 5, False)


# ------------------------------------------------------------------ planification, fuseau
def test_interval_minute_field():
    assert schedule.build_specs({"mode": "interval", "interval": "2h", "interval_minute": "10"}) == ["*-*-* 0/2:10:00"]


def test_validate_timezone():
    assert schedule.validate_timezone("Africa/Algiers") == "Africa/Algiers"
    for bad in ("../etc/passwd", "Africa/Nulle_Part", "", "UTC; rm"):
        with pytest.raises(OdooGuardError):
            schedule.validate_timezone(bad)


# ------------------------------------------------------------------ test d'un seul canal
def test_dispatch_only_one_channel(cfg, monkeypatch):
    from odooguard import notify
    sent = []
    for name, cls in notify.CHANNELS.items():
        monkeypatch.setattr(cls, "send", lambda self, *a, n=name: sent.append(n))
    cfg.alerts.channels = ["journal", "email", "telegram"]
    assert notify.dispatch(cfg, "warning", "t", "b", only="telegram") == {"telegram": "ok"}
    assert sent == ["telegram"]


def test_notify_test_telegram_requires_config(cfg):
    cfg.alerts.telegram_token_file, cfg.alerts.telegram_chat_ids = "", []
    with pytest.raises(OdooGuardError, match="Telegram non configuré"):
        settings.job_notify_test(cfg, None, "telegram")


# ------------------------------------------------------------------ pages web
@pytest.fixture
def web(cfg, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from odooguard.web import app as app_mod
    from odooguard.web import auth
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


def test_security_page(web):
    client, hist = web
    now = time.time()
    hist.security_add([{"ts": now - 60, "kind": "login_failed", "db": "P", "login": "root<script>", "ip": "6.6.6.6"},
                       {"ts": now - 30, "kind": "user_deleted", "db": "P", "login": "parti", "detail": "Parti"}])
    hist.kv_put("odoo_users:" + "LNHC_Prod_2025", {"2": {"login": "admin", "active": True, "share": False,
                                                       "created": now, "name": "Admin"}})
    html = client("lea").get("/securite").text
    assert "6.6.6.6" in html and "Compte supprimé" in html and "parti" in html
    assert "root&lt;script&gt;" in html and "<script>" not in html.split("</head>")[1]
    assert 'href="/securite" aria-current="page"' in html


def test_dashboard_widgets(web, cfg):
    client, hist = web
    st = {"generated_at": time.time(), "overall": "ok", "rpo": {}, "components": [
        {"name": "db", "level": "ok", "message": "ok"},
        {"name": "stockage:odoo:/", "level": "warning", "group": "monitor", "message": "x"}],
        "monitor": {"collected_at": time.time(), "thresholds": {"warn": "80", "crit": "90"},
                    "storage": [{"host": "nas", "label": "nas1", "mount": "//192.168.8.30/odoo", "total": 100,
                                 "used": 85, "free": 15, "pct": 85.0, "level": "warning", "odooguard_bytes": 40}],
                    "services": [{"host": "odoo", "unit": "odoo.service", "active": "failed", "level": "critical"}],
                    "failed_units": {}, "security": {"failed_24h": 3, "ok_24h": 9}}}
    with open(cfg.status_file, "w") as fh:
        json.dump(st, fh)
    html = client("lea").get("/").text
    assert "Stockage" in html and "//192.168.8.30/odoo" in html and 'value="85.0"' in html
    assert "disk-warning" in html and "odoo</strong>" in html
    assert "stockage:odoo:/" not in html                  # pas dans le tableau des sauvegardes
    assert "3</strong> connexion(s) échouée(s)" in html


def test_surveillance_and_timezone_forms(web):
    client, hist = web
    assert client("lea").post("/surveillance/save", data={}).status_code in (400, 403)
    ada = client("ada")
    page = ada.get("/surveillance").text
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = ada.post("/surveillance/save", data={"csrf": tok, "disk_warn_pct": "75", "disk_crit_pct": "88",
                                              "services_local": "odooguard-web", "failed_logins_warn": "8",
                                              "failed_logins_window": "15", "keep_days": "90",
                                              "security": "on"}, follow_redirects=False)
    assert r.status_code == 303
    job = hist.jobs_recent(1)[0]
    assert job["kind"] == "monitoring-save" and json.loads(job["params"])["disk_warn_pct"] == 75
    plan = ada.get("/planification").text
    assert "Fuseau horaire" in plan and 'value="Africa/Algiers"' in plan
    r = ada.post("/planification/timezone", data={"csrf": tok, "timezone": "Africa/Algiers"}, follow_redirects=False)
    assert r.status_code == 303 and json.loads(hist.jobs_recent(1)[0]["params"]) == {
        "action": "timezone", "timezone": "Africa/Algiers"}
    form = ada.get("/planification/odooguard-copy.timer").text
    assert 'data-schedule-form' in form and "/static/schedule.js" in form and 'name="interval_minute"' in form
    notif = ada.get("/notifications").text
    assert 'name="channel" value="telegram"' in notif


def test_apache2_not_watched():
    svcs = [{"unit": "nginx.service", "active": "active"}, {"unit": "apache2.service", "active": "inactive"},
            {"unit": "odoo.service", "active": "inactive"}]
    assert [s["unit"] for s in monitor.drop_unwatched(svcs)] == ["nginx.service", "odoo.service"]


def test_set_option_keeps_section(cfg):
    from odooguard import config as config_mod
    before = config_mod.load(cfg.path)
    settings.job_databases_save(cfg, {"databases": ["LNHC_Prod_2025"]})
    after = config_mod.load(cfg.path)
    assert after.databases == ["LNHC_Prod_2025"]
    assert after.file_sources == before.file_sources and after.source.host == before.source.host
    with pytest.raises(OdooGuardError):
        settings.validate_databases(["postgres"])
    with pytest.raises(OdooGuardError):
        settings.validate_databases(["x; drop"])


def test_database_list_parses_info(cfg, monkeypatch):
    from odooguard import pg
    from odooguard.util import Result
    data = [{"name": cfg.stanza, "backup": [{"label": "L", "database-ref": [
        {"name": "postgres", "oid": 5}, {"name": "LNHC_Prod_2025", "oid": 16384},
        {"name": "LNHC_08_07_26", "oid": 16385}, {"name": "template1", "oid": 1}]}]}]
    monkeypatch.setattr(pg.PgBackRest, "exec", lambda self, *a, **k: Result(0, json.dumps(data), "", 0))
    assert pg.PgBackRest(cfg).database_list("L") == ["LNHC_08_07_26", "LNHC_Prod_2025"]


def test_bases_page(web, monkeypatch):
    client, hist = web
    from odooguard import pg
    monkeypatch.setattr(pg.PgBackRest, "database_list", lambda self, label=None: ["LNHC_Prod_2025", "Autre"])
    ada = client("ada")
    html = ada.get("/bases").text
    assert 'value="Autre"' in html and "toute l'instance PostgreSQL" in html
    tok = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    r = ada.post("/bases/save", data={"csrf": tok, "db": ["LNHC_Prod_2025", "Autre"], "other": ""},
                 follow_redirects=False)
    assert r.status_code == 303
    job = hist.jobs_recent(1)[0]
    assert job["kind"] == "databases-save" and json.loads(job["params"])["databases"] == ["LNHC_Prod_2025", "Autre"]


# ------------------------------------------------------------------ envoi par type de notification
def test_routes_per_category(cfg, monkeypatch):
    from odooguard import notify
    sent = []
    for name, cls in notify.CHANNELS.items():
        monkeypatch.setattr(cls, "send", lambda self, lvl, title, body, n=name: sent.append((n, title)))
    cfg.alerts.channels = ["journal", "email", "telegram"]
    cfg.alerts.routes = {"stockage": ["telegram"], "comptes": []}
    assert set(notify.dispatch(cfg, "critical", "x", "b", category="stockage")) == {"journal", "telegram"}
    assert set(notify.dispatch(cfg, "critical", "x", "b", category="comptes")) == {"journal"}
    assert set(notify.dispatch(cfg, "critical", "x", "b", category="echec")) == {"journal", "email", "telegram"}
    # une catégorie ne peut pas réactiver un canal désactivé globalement
    cfg.alerts.channels = ["journal", "email"]
    cfg.alerts.routes = {"stockage": ["telegram", "email"]}
    assert set(notify.dispatch(cfg, "critical", "x", "b", category="stockage")) == {"journal", "email"}


def test_alerter_one_message_per_category(cfg, monkeypatch):
    from odooguard import notify
    calls = []
    monkeypatch.setattr(notify, "dispatch", lambda c, lvl, title, body, **k: calls.append((k["category"], title)) or {})
    st = {"overall": "critical", "rpo": {}, "components": [
        {"name": "db", "level": "critical", "message": "trop ancienne"},
        {"name": "stockage:odoo:/", "level": "warning", "message": "85 %"},
        {"name": "service:odoo:odoo.service", "level": "critical", "message": "failed"},
        {"name": "securite:odoo", "level": "ok", "message": "0"}]}
    notify.Alerter(cfg, History(cfg.history_db)).process(st)
    assert sorted(c for c, _ in calls) == ["etat", "services", "stockage"]
    assert ("stockage", "1 alerte(s) d'espace disque") in calls


def test_routes_saved_and_loaded(cfg):
    from odooguard import config as config_mod
    p = {"min_level": "warning", "repeat_hours": "4", "routes_present": True,
         "route_stockage_telegram": True, "route_echec_email": True, "route_echec_telegram": True}
    settings.job_notify_save(cfg, p)
    r = config_mod.load(cfg.path).alerts.routes
    assert r["stockage"] == ["telegram"] and r["echec"] == ["email", "telegram"] and r["comptes"] == []


def test_notifications_page_routes(web):
    client, hist = web
    ada = client("ada")
    html = ada.get("/notifications").text
    assert 'name="route_stockage_telegram"' in html and "Quoi envoyer" in html
    tok = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    r = ada.post("/notifications/save", data={"csrf": tok, "min_level": "warning", "repeat_hours": "4", "smtp_security": "starttls",
                                               "routes_present": "1", "route_intrusion_telegram": "on"},
                 follow_redirects=False)
    assert r.status_code == 303, re.findall(r'role="alert">([^<]*)', r.text)
    params = json.loads(hist.jobs_recent(1)[0]["params"])
    assert params["route_intrusion_telegram"] is True and params["route_intrusion_email"] is False


# ------------------------------------------------------------------ NAS séparés, suspension
def test_nas_categories_and_routes(cfg, monkeypatch):
    from odooguard import config as config_mod
    from odooguard import notify
    from odooguard.config import notify_categories
    cats = notify_categories(cfg)
    assert {"nas-nas1", "nas-nas2"} <= set(cats)
    assert notify.category_of("nas:nas2") == "nas-nas2" and notify.category_of("copy:nas1") == "nas-nas1"
    assert notify.category_of("stockage:nas:nas1") == "stockage"
    p = {"min_level": "warning", "repeat_hours": "4", "routes_present": True,
         "route_nas-nas1_email": True, "route_nas-nas1_telegram": True}
    settings.job_notify_save(cfg, p)
    r = config_mod.load(cfg.path).alerts.routes
    assert r["nas-nas1"] == ["email", "telegram"] and r["nas-nas2"] == []


def test_nas_suspend_resolves_its_alerts(cfg, monkeypatch):
    from odooguard import config as config_mod
    from odooguard import notify
    hist = History(cfg.history_db)
    sent = []
    monkeypatch.setattr(notify, "dispatch", lambda c, lvl, title, body, **k: sent.append((k["category"], title, body)) or {})
    notify.Alerter(cfg, hist).process({"components": [{"name": "nas:nas2", "level": "critical", "message": "absent"},
                                                      {"name": "db", "level": "ok"}]})
    assert sent[-1][0] == "nas-nas2"
    settings.job_nas_enable(cfg, {"name": "nas2", "enabled": False})
    assert config_mod.load(cfg.path).destinations["nas2"].enabled is False
    sent.clear()
    notify.Alerter(cfg, hist, now=time.time() + 60).process({"components": [{"name": "db", "level": "ok"}]})
    assert hist.alerts_active() == []
    assert sent and sent[0][0] == "nas-nas2" and "n'est plus surveillé" in sent[0][2]
    with pytest.raises(OdooGuardError):
        settings.job_nas_enable(cfg, {"name": "inconnu", "enabled": True})


def test_nas_page_suspend_button(web):
    client, hist = web
    ada = client("ada")
    html = ada.get("/nas").text
    assert 'action="/nas/nas2/enable"' in html and ">Suspendre<" in html
    tok = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    r = ada.post("/nas/nas2/enable", data={"csrf": tok, "enabled": "0"}, follow_redirects=False)
    assert r.status_code == 303
    job = hist.jobs_recent(1)[0]
    assert job["kind"] == "nas-enable" and json.loads(job["params"]) == {"name": "nas2", "enabled": False}
    notif = ada.get("/notifications").text
    assert 'name="route_nas-nas2_telegram"' in notif


# ------------------------------------------------------------------ VM de test
TESTVM = {"label": "VM de recette", "host": "192.168.8.90", "odoo_url": "http://192.168.8.90:8069",
          "pg_version": "15", "pg_cluster": "main", "odoo_service": "odoo", "odoo_conf": "/etc/odoo/odoo.conf",
          "filestore_path": "/var/lib/odoo/.local/share/Odoo/filestore"}


def test_testvm_save_and_refuse_production(cfg):
    from odooguard import config as config_mod
    with pytest.raises(OdooGuardError, match="production"):
        settings.validate_testvm(cfg, {**TESTVM, "host": cfg.source.host})
    with pytest.raises(OdooGuardError):
        settings.validate_testvm(cfg, {**TESTVM, "odoo_conf": "/etc/odoo.conf; rm -rf /"})
    res = settings.job_testvm_save(cfg, TESTVM)
    assert "NOUVELLE ADRESSE" in res["message"] and "ssh-keyscan 192.168.8.90" in res["message"]
    t = config_mod.load(cfg.path).targets[cfg.restore_test.target]
    assert (t.label, t.host, t.is_production, t.ssh_key) == ("VM de recette", "192.168.8.90", False,
                                                             cfg.targets[cfg.restore_test.target].ssh_key)


def test_testvm_connection_messages(cfg, monkeypatch):
    from odooguard.util import CommandError
    from odooguard import remote

    def failing(self, verb, *a, **k):
        raise CommandError(["ssh"], 255, "Host key verification failed.")
    monkeypatch.setattr(remote.Agent, "call", failing)
    with pytest.raises(OdooGuardError, match="ssh-keyscan"):
        settings.job_testvm_test(cfg)
    from odooguard.util import Result
    monkeypatch.setattr(remote.Agent, "call", lambda self, verb, *a, **k: Result(0, "active\n", "", 0))
    monkeypatch.setattr(remote.Agent, "call_json", lambda self, verb, *a, **k: {"databases": ["ogtest_1"]})
    assert "Odoo : active" in settings.job_testvm_test(cfg)["message"]


def test_testvm_page(web):
    client, hist = web
    ada = client("ada")
    html = ada.get("/vm-test").text
    assert 'name="host"' in html and "Tester la connexion" in html
    tok = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    r = ada.post("/vm-test/save", data={"csrf": tok, **TESTVM}, follow_redirects=False)
    assert r.status_code == 303 and hist.jobs_recent(1)[0]["kind"] == "testvm-save"
    assert "Tester la connexion" not in client("lea").get("/vm-test").text


def test_age_message_consistent_with_level():
    from odooguard.status import _age_message, _level_for_age
    for age, lvl in ((600, "ok"), (3000, "warning"), (12600, "critical")):
        assert _level_for_age(age, 3600) == lvl
        msg = _age_message("sauvegarde des fichiers", age, 3600)
        assert (msg == "ok") == (lvl == "ok")
    assert _age_message("sauvegarde des fichiers", 12600, 3600) == \
        "EN RETARD : dernière sauvegarde des fichiers il y a 3 h 30 (seuil 60 min)"


def test_thresholds_roundtrip(cfg):
    from odooguard import config as config_mod
    settings.job_thresholds_save(cfg, {"wal_max_lag": "10", "db_any_max_age": "2", "db_full_max_age": "8",
                                       "files_max_age": "1", "copy_max_age": "26", "restore_test_max_age": "15"})
    c = config_mod.load(cfg.path)
    assert c.max_age == {"wal": 600, "db_any": 7200, "db_full": 8 * 86400, "files": 3600, "copy": 26 * 3600}
    assert c.restore_test.max_age == 15 * 86400
    with pytest.raises(OdooGuardError):
        settings.validate_thresholds({"wal_max_lag": "0", "db_any_max_age": "2", "db_full_max_age": "8",
                                      "files_max_age": "1", "copy_max_age": "26", "restore_test_max_age": "8"})


def test_thresholds_form(web):
    client, hist = web
    ada = client("ada")
    html = ada.get("/surveillance").text
    assert 'name="copy_max_age"' in html
    tok = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    r = ada.post("/surveillance/seuils", data={"csrf": tok, "wal_max_lag": "10", "db_any_max_age": "2",
                                               "db_full_max_age": "8", "files_max_age": "1", "copy_max_age": "26",
                                               "restore_test_max_age": "8"},
                 follow_redirects=False)
    assert r.status_code == 303 and hist.jobs_recent(1)[0]["kind"] == "thresholds-save"


def test_retention_roundtrip_and_backup_args(cfg, monkeypatch):
    from odooguard import config as config_mod
    from odooguard import pg
    with pytest.raises(OdooGuardError):
        settings.validate_retention({"retention_full": "4", "retention_diff": "7", "keep_hourly": "0",
                                     "keep_daily": "0", "keep_weekly": "0", "keep_monthly": "0"})
    settings.job_retention_save(cfg, {"retention_full": "6", "retention_diff": "10", "keep_hourly": "24",
                                      "keep_daily": "30", "keep_weekly": "8", "keep_monthly": "24"})
    c = config_mod.load(cfg.path)
    assert (c.retention_full, c.retention_diff) == (6, 10)
    assert c.restic_keep == {"hourly": "24", "daily": "30", "weekly": "8", "monthly": "24"}
    assert c.file_sources == cfg.file_sources
    seen = []
    monkeypatch.setattr(pg.PgBackRest, "exec", lambda self, *a, **k: seen.append(a))
    pg.PgBackRest(c).backup("incr")
    assert "--repo1-retention-full=6" in seen[0] and "--repo1-retention-diff=10" in seen[0]
    assert seen[0][-2:] == ("--type=incr", "backup")


def test_retention_page(web):
    client, hist = web
    ada = client("ada")
    html = ada.get("/retention").text
    assert 'name="retention_full"' in html and 'name="keep_monthly"' in html
    tok = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    r = ada.post("/retention/save", data={"csrf": tok, "retention_full": "4", "retention_diff": "7",
                                          "keep_hourly": "48", "keep_daily": "14", "keep_weekly": "8",
                                          "keep_monthly": "12"}, follow_redirects=False)
    assert r.status_code == 303 and hist.jobs_recent(1)[0]["kind"] == "retention-save"


def test_externals_toggle(cfg, web):
    from odooguard import config as config_mod
    settings.job_externals_save(cfg, {"enabled": ["veeam"]})
    ext = config_mod.load(cfg.path).externals
    assert ext["veeam"].enabled and not ext["offline"].enabled
    with pytest.raises(OdooGuardError):
        settings.job_externals_save(cfg, {"enabled": ["inconnu"]})
    client, hist = web
    ada = client("ada")
    html = ada.get("/surveillance").text
    assert 'value="offline"' in html
    tok = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    r = ada.post("/surveillance/externes", data={"csrf": tok, "ext": ["veeam"]}, follow_redirects=False)
    assert r.status_code == 303 and json.loads(hist.jobs_recent(1)[0]["params"]) == {"enabled": ["veeam"]}


JOURNAL = """odooguard : PWD=/ ; USER=root ; COMMAND=/usr/local/sbin/odooguard-schedule-helper set odooguard-copy.timer '*-*-* *:15:00'
pam_unix(sudo:session): session opened for user root(uid=0) by (uid=999)
pam_unix(sudo:session): session closed for user root
2026-11-25 14:41:42,559 INFO Copie vers les NAS : planification enregistrée (*-*-* *:15:00)
{"ts": "2026-11-25T14:41:42+0100", "actor": "web:admin", "action": "set"}
2026-11-25 14:43:21,722 INFO NAS nas2 retiré (les sauvegardes déjà présentes sur le NAS sont conservées)
2026-11-25 14:50:01,100 ERROR échec : rsync en erreur (23)
Stopping odooguard-jobs.service - OdooGuard - exécution des travaux de l'assistant web...
odooguard-jobs.service: Main process exited, code=exited, status=2/n/a
odooguard-jobs.service: Failed with result 'exit-code'.
odooguard-jobs.service: Triggering OnFailure= dependencies.
odooguard-jobs.service: Consumed 3.163s CPU time, 54.8M memory peak, 0B memory swap peak.
"""


def test_summarize_unit_journal():
    from odooguard.cli import summarize_unit_journal
    cause, lines = summarize_unit_journal(JOURNAL)
    assert cause == "14:50:01 ERROR échec : rsync en erreur (23)"
    assert lines == ["14:41:42 Copie vers les NAS : planification enregistrée (*-*-* *:15:00)",
                     "14:43:21 NAS nas2 retiré (les sauvegardes déjà présentes sur le NAS sont conservées)",
                     "14:50:01 ERROR échec : rsync en erreur (23)"]


def test_sigterm_exits_cleanly(monkeypatch):
    import signal
    from odooguard import cli
    def boom(ctx):
        cli._sigterm(signal.SIGTERM, None)
    monkeypatch.setattr(cli, "Ctx", lambda args: None)
    parser = cli.build_parser()
    args = parser.parse_args(["status"])
    args.func = boom
    monkeypatch.setattr(cli, "build_parser", lambda: type("P", (), {"parse_args": lambda self, a: args})())
    cli._STOPPED_BY_SYSTEMD[0] = False
    assert cli.main([]) == 0
    cli._STOPPED_BY_SYSTEMD[0] = False


def test_failure_mail_is_short():
    from odooguard.cli import failure_body, what_fr
    from odooguard.util import CommandError
    exc = CommandError(["rsync", "-a"], 23, "rsync: [Receiver] write error: Broken pipe (32)\n\nNumber of files: 0\n"
                       "Number of created files: 0\nTotal file size: 0 bytes\nLiteral data: 0 bytes\n"
                       "sent 52 bytes  received 10 bytes  124.00 bytes/sec\ntotal size is 0  speedup is 0.00")
    body = failure_body(exc, "command")
    assert "Broken pipe" in body and "Number of" not in body and "speedup" not in body
    assert what_fr("db", "incr") == "sauvegarde incrémentale de la base"
    assert what_fr("copy:nas1", "copy") == "copie vers nas1"


def test_restore_test_threshold_without_section_key(cfg):
    from odooguard import config as config_mod
    text = open(cfg.path).read()
    i = text.index("[restore_test]")
    j = text.index("max_age", i)
    k = text.index("\n", j)
    open(cfg.path, "w").write(text[:j] + "# (retiré)" + text[k:])
    settings.job_thresholds_save(cfg, {"wal_max_lag": "10", "db_any_max_age": "2", "db_full_max_age": "8",
                                       "files_max_age": "1", "copy_max_age": "26", "restore_test_max_age": "10"})
    assert config_mod.load(cfg.path).restore_test.max_age == 10 * 86400


def test_summary_of_copy_failure_without_noise_or_duplicates():
    from odooguard.cli import summarize_unit_journal
    journal = """copie vers nas1 OK
Failed to start odooguard-copy.service - OdooGuard - copie des dépôts vers les NAS (vérifiée).
2026-11-25 16:59:50,100 INFO copy:nas1 (copy) terminé en 8288 s
2026-11-25 16:59:50,200 ERROR OdooGuard : échec copy:nas2 (copy) [mount] destination nas2 : /mnt/nas-backups n'est pas monté
[CRITIQUE] OdooGuard : échec copy:nas2 (copy) [mount] | destination nas2 : /mnt/nas-backups n'est pas monté
copie vers nas2 ÉCHEC : destination nas2 : /mnt/nas-backups n'est pas monté
copie vers nas1 OK
"""
    cause, lines = summarize_unit_journal(journal)
    assert cause == "copie vers nas2 ÉCHEC : destination nas2 : /mnt/nas-backups n'est pas monté"
    assert lines.count("copie vers nas1 OK") == 1
    assert not any(l.startswith(("[CRITIQUE]", "Failed to start")) for l in lines)


def test_copy_skips_nas_removed_during_run(cfg, monkeypatch, capsys):
    from odooguard import cli, dest as dest_mod
    calls = []
    monkeypatch.setattr(dest_mod, "preflight", lambda d: calls.append(d.name) or {})
    monkeypatch.setattr(cli, "tracked", lambda ctx, comp, kind, job, **k: calls.append(comp) or {})
    settings.job_nas_enable(cfg, {"name": "nas2", "enabled": False})   # suspendu après le démarrage
    import types
    ctx = types.SimpleNamespace(cfg=cfg, args=types.SimpleNamespace(what="copy", dest=None, wait=0), history=None)
    cfg.destinations["nas2"].enabled = True   # la copie en cours croit encore nas2 actif
    try:
        cli.cmd_backup(ctx)
    except Exception:
        pass
    assert "copy:nas2" not in calls and "copy:nas1" in calls


def test_copy_holds_db_lock_only_for_final_pass(cfg, monkeypatch):
    import types
    from odooguard import cli, dest as dest_mod, util
    events = []
    held = set()
    real_lock = util.file_lock

    from contextlib import contextmanager

    @contextmanager
    def fake_lock(lock_dir, name, wait=0, holder=""):
        held.add(name); events.append(("lock", name))
        try:
            yield
        finally:
            held.discard(name)
    monkeypatch.setattr(cli, "file_lock", fake_lock)
    monkeypatch.setattr(dest_mod, "preflight", lambda d: {})
    monkeypatch.setattr(dest_mod, "copy_pgbackrest",
                        lambda c, d, progress=None, verify=True: events.append(("pg", verify, "db" in held)) or {"label": "L"})
    monkeypatch.setattr(dest_mod, "copy_restic", lambda c, d: events.append(("restic", "files" in held)) or {"snapshots": 1})
    monkeypatch.setattr(dest_mod, "write_manifest", lambda c, d, l, n: events.append(("man", set(held))) or {"total_bytes": 1})
    monkeypatch.setattr(cli, "tracked", lambda ctx, comp, kind, job, **k: job())
    ctx = types.SimpleNamespace(cfg=cfg, args=types.SimpleNamespace(what="copy", dest="nas1", wait=0), history=None)
    cli.cmd_backup(ctx)
    assert ("pg", False, False) in events          # passe principale sans verrou
    assert ("pg", True, True) in events            # passe finale vérifiée sous le verrou db
    assert ("restic", True) in events and ("man", set()) in events


def test_copy_pgbackrest_ignores_nas_dates(cfg, tmp_path, monkeypatch):
    """NAS qui ne garde pas les dates : rien n'est renvoyé, mais les index modifiés le sont."""
    import shutil
    from odooguard import dest as dest_mod
    from odooguard.config import Destination
    repo = tmp_path / "repo"
    (repo / "backup/lnhc/F1").mkdir(parents=True)
    (repo / "archive/lnhc").mkdir(parents=True)
    (repo / "backup/lnhc/F1/data.zst").write_bytes(b"x" * 5000)
    (repo / "backup/lnhc/backup.info").write_text("AAAA")
    (repo / "archive/lnhc/archive.info").write_text("v1")
    cfg.repo_path = str(repo)
    d = Destination(name="nas1", path=str(tmp_path / "nas"))
    calls = []
    real_run, real_stream = dest_mod.run, dest_mod.run_streaming
    monkeypatch.setattr(dest_mod, "run", lambda cmd, **k: calls.append(cmd) or real_run(cmd, **k))
    dest_mod.copy_pgbackrest(cfg, d, verify=False)
    target = tmp_path / "nas/pgbackrest"
    assert (target / "backup/lnhc/F1/data.zst").stat().st_size == 5000
    assert (target / "backup/lnhc/backup.info").read_text() == "AAAA"
    # Le NAS « change » toutes les dates ; un index est réécrit à taille égale
    for f in target.rglob("*"):
        if f.is_file():
            os.utime(f, (1, 1))
    (repo / "backup/lnhc/backup.info").write_text("BBBB")
    out = []
    monkeypatch.setattr(dest_mod, "run_streaming",
                        lambda cmd, cb, **k: out.append(real_stream(cmd, cb, **k)) or out[-1])
    dest_mod.copy_pgbackrest(cfg, d, verify=False)
    assert "Number of regular files transferred: 0" in out[-1].stdout   # données non renvoyées
    assert (target / "backup/lnhc/backup.info").read_text() == "BBBB"   # index recopié


def test_lock_names_holder_and_redundant_backup_skipped(cfg, tmp_path, monkeypatch):
    import types
    from odooguard import cli
    from odooguard.util import LockBusy, file_lock
    with file_lock(str(tmp_path), "db", holder="sauvegarde diff de la base"):
        with pytest.raises(LockBusy) as e:
            with file_lock(str(tmp_path), "db", wait=0):
                pass
        assert "sauvegarde diff de la base" in str(e.value) and e.value.holder["pid"]
    cfg.lock_dir = str(tmp_path)
    ran = []
    monkeypatch.setattr(cli, "tracked", lambda *a, **k: ran.append(a[1:3]) or {"label": "L", "added": 0})

    def backup(kind):
        ctx = types.SimpleNamespace(cfg=cfg, args=types.SimpleNamespace(what="db", type=kind, wait=0), history=None)
        return cli.cmd_backup(ctx)
    with file_lock(str(tmp_path), "db", holder="sauvegarde diff de la base"):
        assert backup("incr") == 0                    # redondante : ignorée, sans erreur
        with pytest.raises(LockBusy):
            backup("full")                            # plus complète : vraie erreur
    with file_lock(str(tmp_path), "db", holder="copie vers nas1 (passe finale)"):
        with pytest.raises(LockBusy, match="copie vers nas1"):
            backup("incr")
    assert ran == []
