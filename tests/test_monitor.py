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
