import json
import smtplib

import pytest

from odooguard import notify
from odooguard.history import History


@pytest.fixture
def hist(cfg):
    return History(cfg.history_db)


def status(**levels):
    return {"overall": max(levels.values(), key=notify.LEVELS.get, default="ok"),
            "rpo": {"db_seconds": 3, "files_seconds": 900},
            "components": [{"name": k, "level": v, "message": f"msg {k}"} for k, v in levels.items()]}


class Recorder(notify.Channel):
    name = "rec"
    sent = []

    def send(self, level, title, body):
        Recorder.sent.append((level, title, body))


@pytest.fixture
def rec(cfg, monkeypatch):
    Recorder.sent = []
    monkeypatch.setitem(notify.CHANNELS, "rec", Recorder)
    cfg.alerts.channels = ["journal", "rec"]
    return Recorder


def test_no_alert_when_all_ok(cfg, hist, rec):
    assert notify.Alerter(cfg, hist).process(status(db="ok", wal="ok")) is None
    assert rec.sent == []


def test_new_problem_then_silence_then_reminder_then_resolved(cfg, hist, rec):
    t = 1_000_000.0
    notify.Alerter(cfg, hist, now=t).process(status(db="critical", wal="ok"))
    assert len(rec.sent) == 1 and rec.sent[0][0] == "critical"
    assert "db [CRITIQUE, nouveau]" in rec.sent[0][2]
    # 5 min plus tard, même état : pas de nouvel envoi (anti-rafale)
    notify.Alerter(cfg, hist, now=t + 300).process(status(db="critical", wal="ok"))
    assert len(rec.sent) == 1
    # 4 h plus tard : rappel
    notify.Alerter(cfg, hist, now=t + 4 * 3600 + 1).process(status(db="critical", wal="ok"))
    assert len(rec.sent) == 2 and "rappel" in rec.sent[1][2]
    # Résolu
    notify.Alerter(cfg, hist, now=t + 5 * 3600).process(status(db="ok", wal="ok"))
    assert len(rec.sent) == 3 and rec.sent[2][0] == "ok" and "résolu" in rec.sent[2][2]
    assert hist.alerts_active() == []


def test_escalation_is_notified(cfg, hist, rec):
    notify.Alerter(cfg, hist, now=1000).process(status(files="warning"))
    notify.Alerter(cfg, hist, now=1300).process(status(files="critical"))
    assert len(rec.sent) == 2 and "aggravé" in rec.sent[1][2]


def test_grouped_in_one_message(cfg, hist, rec):
    """Un message par type : sauvegardes ensemble, chaque NAS à part (réglages d'envoi séparés)."""
    notify.Alerter(cfg, hist).process(status(db="critical", **{"nas:nas1": "critical", "copy:nas1": "warning",
                                                               "wal": "warning"}))
    titles = sorted(t for _, t, *_ in rec.sent)
    assert titles == ["2 problème(s) de sauvegarde", "2 problème(s) nas1"]


def test_min_level_critical_skips_warning(cfg, hist, rec):
    cfg.alerts.min_level = "critical"
    assert notify.Alerter(cfg, hist).process(status(files="warning")) is None
    assert rec.sent == []


def test_failing_channel_does_not_block_others(cfg, hist, rec, monkeypatch):
    class Broken(notify.Channel):
        name = "broken"

        def send(self, *a):
            raise RuntimeError("SMTP en panne")
    monkeypatch.setitem(notify.CHANNELS, "broken", Broken)
    cfg.alerts.channels = ["broken", "rec"]
    res = notify.dispatch(cfg, "critical", "t", "b", history=hist)
    assert res["broken"].startswith("erreur") and res["rec"] == "ok" and res["journal"] == "ok"
    logged = hist.notifications(1)[0]
    assert json.loads(logged["channels"])["broken"].startswith("erreur")


def test_email_channel(cfg, monkeypatch, tmp_path):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["host"] = (host, port)

        def starttls(self, context):
            sent["tls"] = True

        def login(self, user, pw):
            sent["login"] = (user, pw)

        def send_message(self, msg):
            sent["msg"] = msg

        def quit(self):
            pass
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    pw = tmp_path / "smtp.pass"
    pw.write_text("s3cret\n")
    a = cfg.alerts
    a.smtp_host, a.smtp_user, a.smtp_password_file = "smtp.lan", "og", str(pw)
    a.mail_from, a.mail_to = "og@lan", ["it@lan", "dsi@lan"]
    notify.EmailChannel(cfg).send("critical", "NAS absent", "détail")
    assert sent["tls"] and sent["login"] == ("og", "s3cret")
    assert sent["msg"]["Subject"] == "[OdooGuard LNHC] CRITIQUE : NAS absent"
    assert sent["msg"]["To"] == "it@lan, dsi@lan"


def test_email_not_configured(cfg):
    cfg.alerts.smtp_host = ""
    with pytest.raises(RuntimeError, match="SMTP non configuré"):
        notify.EmailChannel(cfg).send("critical", "t", "b")


def test_telegram_channel(cfg, monkeypatch, tmp_path):
    calls = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Opener:
        def open(self, req, timeout):
            calls.append((req.full_url, req.data.decode()))
            return Resp(b'{"ok": true}')
    monkeypatch.setattr(notify.urllib.request, "build_opener", lambda *h: Opener())
    tok = tmp_path / "tg"
    tok.write_text("123:ABC")
    cfg.alerts.telegram_token_file = str(tok)
    cfg.alerts.telegram_chat_ids = ["-1001", "42"]
    notify.TelegramChannel(cfg).send("warning", "titre", "corps")
    assert len(calls) == 2
    assert calls[0][0] == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert "chat_id=-1001" in calls[0][1]


def test_telegram_error_hides_token(cfg, monkeypatch, tmp_path):
    import urllib.error

    class Opener:
        def open(self, req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
    monkeypatch.setattr(notify.urllib.request, "build_opener", lambda *h: Opener())
    tok = tmp_path / "tg"
    tok.write_text("123:SECRET")
    cfg.alerts.telegram_token_file = str(tok)
    cfg.alerts.telegram_chat_ids = ["1"]
    with pytest.raises(RuntimeError) as exc:
        notify.TelegramChannel(cfg).send("warning", "t", "b")
    assert "SECRET" not in str(exc.value)
