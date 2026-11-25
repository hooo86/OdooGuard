"""Application web OdooGuard (phase 2 : consultation ; phase 3 : assistant de restauration).

Lancement (service ``odooguard-web``) :
    odooguard web          # uvicorn en HTTPS sur [web] listen:port

Le tableau de bord LIT l'état calculé toutes les 5 min par ``odooguard status
--record`` (status.json) et l'historique SQLite : il ne lance aucune
opération et peut s'arrêter sans affecter les sauvegardes (systemd timers).
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from odooguard import __version__, external, jobs
from odooguard.config import Config
from odooguard.history import History
from odooguard.util import OdooGuardError, fmt_age, fmt_size, fmt_time
from odooguard.web import auth
from odooguard.web.charts import line_chart

log = logging.getLogger("odooguard.web")
HERE = Path(__file__).parent
STALE_AFTER = 15 * 60
LEVEL_FR = {"ok": "OK", "warning": "Avertissement", "critical": "Critique", "unknown": "Inconnu"}
LEVEL_ICON = {"ok": "✓", "warning": "!", "critical": "✕", "unknown": "?"}
COMPONENT_FR = {
    "wal": "Flux WAL (base, temps réel)", "db": "Base PostgreSQL (pgBackRest)",
    "files": "Filestore / modules / config (restic)", "alerting": "Chaîne d'alerte",
    "disk:repo": "Disque des dépôts", "disk:staging": "Zone de restauration",
    "restore-test": "Test de restauration hebdomadaire",
}


def component_label(c: dict) -> str:
    n = c["name"]
    if c.get("label"):
        return c["label"]
    if n in COMPONENT_FR:
        return COMPONENT_FR[n]
    if n.startswith("copy:"):
        return f"Copie vers {n[5:]}"
    if n.startswith("nas:"):
        return f"Accès {n[4:]}"
    return n


ACCOUNT_KINDS = {"user_created": "Compte créé", "user_deleted": "Compte supprimé",
                 "user_archived": "Compte archivé", "user_reactivated": "Compte réactivé",
                 "user_renamed": "Identifiant modifié"}


def create_app(cfg: Config, history: History | None = None) -> FastAPI:
    history = history or History(cfg.history_db)
    secret = auth.load_secret(cfg.web.secret_file)
    throttle = auth.Throttle()
    app = FastAPI(title="OdooGuard", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals.update(fmt_age=fmt_age, fmt_size=fmt_size, fmt_time=fmt_time,
                                 LEVEL_FR=LEVEL_FR, LEVEL_ICON=LEVEL_ICON, site=cfg.site,
                                 version=__version__, label=component_label)
    templates.env.filters["fromjson"] = json.loads

    def icon(name: str) -> Markup:
        """Icône du jeu servi localement (/static/icons.svg) ; nom = littéral des gabarits."""
        safe = "".join(c for c in name if c.isalnum() or c == "_")
        return Markup(f'<svg class="icon" aria-hidden="true"><use href="/static/icons.svg#i-{safe}"></use></svg>')
    templates.env.globals["icon"] = icon
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # ------------------------------------------------------------ outils
    def current_user(request: Request) -> str | None:
        user = auth.read_session(secret, request.cookies.get(auth.COOKIE))
        if user and user in auth.read_users(cfg.web.users_file):
            return user
        return None

    def role_of(user: str | None) -> str:
        return auth.read_accounts(cfg.web.users_file).get(user or "", {}).get("role", "lecteur")

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "?"

    def load_status() -> dict:
        try:
            with open(cfg.status_file, encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            return {"overall": "unknown", "components": [], "rpo": {}, "generated_at": None,
                    "stale": True}
        st["stale"] = time.time() - (st.get("generated_at") or 0) > STALE_AFTER
        if st["stale"]:
            st["overall"] = "unknown"
        return st

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        user = ctx.pop("user", None)
        status = ctx.pop("status_code", 200)
        session = request.cookies.get(auth.COOKIE) or ""
        return templates.TemplateResponse(
            request, name, status_code=status, context={"user": user, "role": role_of(user), "now_ts": time.time(),
                                                         "alerts_count": len(history.alerts_active()) if user else 0,
                            "csrf": auth.csrf_token(secret, session) if session else "", **ctx})

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp: Response = await call_next(request)
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Cache-Control"] = "no-store"
        if request.url.scheme == "https":
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return resp

    def login_required(request: Request):
        user = current_user(request)
        if user is None:
            if request.headers.get("HX-Request"):
                return None, Response(status_code=401, headers={"HX-Redirect": "/login"})
            return None, RedirectResponse("/login", status_code=303)
        return user, None

    # ------------------------------------------------------------ connexion
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return page(request, "login.html", error=None)

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...)):
        ip = client_ip(request)
        if throttle.blocked(ip):
            history.audit("web-login", username[:32], "blocked", {"ip": ip})
            return page(request, "login.html", error="Trop de tentatives : réessayer dans 10 minutes.")
        users = auth.read_users(cfg.web.users_file)
        if username in users and auth.verify_password(password, users[username]):
            throttle.reset(ip)
            history.audit("web-login", username, "success", {"ip": ip})
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(auth.COOKIE, auth.make_session(secret, username, cfg.web.session_hours),
                            max_age=int(cfg.web.session_hours * 3600), httponly=True,
                            secure=request.url.scheme == "https", samesite="strict")
            return resp
        throttle.fail(ip)
        history.audit("web-login", username[:32], "failed", {"ip": ip})
        return page(request, "login.html", error="Identifiant ou mot de passe incorrect.")

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.COOKIE)
        return resp

    # ------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        user, redirect = login_required(request)
        if redirect:
            return redirect
        since = time.time() - 7 * 86400
        charts = [
            line_chart(history.metric_series("rpo_db_seconds", since), chart_id="rpo-db",
                       title="Perte de données maximale — base (7 jours)",
                       threshold=3600, threshold_label="objectif 1 h"),
            line_chart(history.metric_series("rpo_files_seconds", since), chart_id="rpo-files",
                       title="Perte de données maximale — fichiers (7 jours)",
                       threshold=3600, threshold_label="objectif 1 h"),
        ]
        return page(request, "dashboard.html", user=user, st=load_status(), charts=charts,
                    runs=history.recent(15), active=history.alerts_active())

    @app.get("/partials/status", response_class=HTMLResponse)
    def partial_status(request: Request):
        user, redirect = login_required(request)
        if redirect:
            return redirect
        return page(request, "_status.html", user=user, st=load_status(),
                    active=history.alerts_active())

    @app.get("/history", response_class=HTMLResponse)
    def runs(request: Request, component: str = ""):
        user, redirect = login_required(request)
        if redirect:
            return redirect
        rows = history.recent(300, component=f"{component}%" if component else None)
        comps = sorted({r["component"] for r in history.recent(2000)})
        return page(request, "history.html", user=user, runs=rows, component=component,
                    components=comps)

    def progress_list() -> list[dict]:
        from odooguard.progress import read_all
        return read_all(cfg.state_dir)

    @app.get("/partials/progress", response_class=HTMLResponse)
    def partial_progress(request: Request):
        user, redirect = login_required(request)
        if redirect:
            return redirect
        return page(request, "_progress.html", user=user, progress=progress_list())

    @app.post("/backups/start")
    async def backups_start(request: Request):
        form = dict(await request.form())
        user, role, err = require(request, "operateur", form.get("csrf", ""))
        if err:
            return err
        what = form.get("what", "")
        if what not in jobs.BACKUP_UNITS:
            return page(request, "error.html", user=user, message="Sauvegarde inconnue.")
        job_id = history.job_create(user, role, "backup-start", {"what": what})
        history.audit("web-backup-request", jobs.BACKUP_UNITS[what][0], "queued",
                      {"job": job_id, "ip": client_ip(request)})
        return RedirectResponse(f"/backups?lancee={what}&travail={job_id}", status_code=303)

    @app.get("/backups", response_class=HTMLResponse)
    def backups(request: Request):
        user, redirect = login_required(request)
        if redirect:
            return redirect
        db, files, errors = [], [], []
        from odooguard.files import Restic
        from odooguard.pg import PgBackRest
        try:
            info = PgBackRest(cfg).info()
            db = list(reversed(info.backups))
            window = (info.backups[0].stop if info.backups else None, info.wal_max)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pgBackRest : {exc}")
            window = (None, None)
        try:
            from odooguard.files import snapshot_figures
            files = snapshot_figures(history, list(reversed(Restic(cfg).snapshots()))[:100])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"restic : {exc}")
        launched = request.query_params.get("lancee")
        job_id = request.query_params.get("travail", "")
        launched_job = history.job_get(int(job_id)) if job_id.isdigit() else None
        return page(request, "backups.html", user=user, db=db, files=files, errors=errors,
                    window=window, progress=progress_list(), units=jobs.BACKUP_UNITS,
                    launched=jobs.BACKUP_UNITS.get(launched, (None, None))[1], launched_job=launched_job)

    @app.get("/alerts", response_class=HTMLResponse)
    def alerts(request: Request):
        user, redirect = login_required(request)
        if redirect:
            return redirect
        notes = history.notifications(100)
        for n in notes:
            n["channels_d"] = json.loads(n["channels"] or "{}")
        return page(request, "alerts.html", user=user, active=history.alerts_active(),
                    notes=notes, channels=cfg.alerts.channels)

    @app.get("/audit", response_class=HTMLResponse)
    def audit(request: Request):
        user, redirect = login_required(request)
        if redirect:
            return redirect
        rows = history.audit_recent(300)
        for r in rows:
            try:
                r["details_d"] = json.loads(r["details"] or "{}")
            except ValueError:
                r["details_d"] = {}
        return page(request, "audit.html", user=user, rows=rows)

    # ------------------------------------------------------------ API
    @app.get("/healthz")
    def healthz():
        st = load_status()
        return {"web": "ok", "status_fresh": not st["stale"]}

    @app.get("/api/v1/status")
    def api_status(request: Request):
        if current_user(request) is None:
            return JSONResponse({"error": "authentification requise"}, status_code=401)
        return load_status()

    @app.post("/api/v1/report/{name}")
    async def api_report(name: str, request: Request):
        ext = cfg.externals.get(name)
        token = request.headers.get("authorization", "")
        token = token[7:] if token.lower().startswith("bearer ") else ""
        ip = client_ip(request)
        if ext is None or not ext.enabled or not external.check_token(ext, token):
            history.audit("external-report", name[:40], "refused", {"ip": ip})
            return JSONResponse({"error": "refusé"}, status_code=403)
        try:
            body = await request.json()
            stored = external.store(history, name, body, ip)
        except (ValueError, external.ReportError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"stored": True, "status": stored["status"]}

    # ------------------------------------------------------------ phase 3 : assistant
    def require(request: Request, needed: str, form_csrf: str | None = None):
        """(utilisateur, rôle, réponse d'erreur éventuelle)."""
        user, redirect = login_required(request)
        if redirect:
            return None, None, redirect
        role = role_of(user)
        if not jobs.can(role, needed):
            return user, role, page(request, "error.html", user=user, status_code=403,
                                    message=f"Cette page exige le rôle « {needed} » (vous : {role}).")
        if form_csrf is not None and not auth.check_csrf(secret, request.cookies.get(auth.COOKIE), form_csrf):
            return user, role, page(request, "error.html", user=user,
                                    message="Jeton de formulaire invalide : recharger la page.")
        return user, role, None

    _window_cache: dict = {}

    def pitr_window() -> tuple[float | None, str | None]:
        """Fenêtre de restauration (plus ancienne sauvegarde -> maintenant), cache 60 s."""
        if time.time() - _window_cache.get("ts", 0) < 60:
            return _window_cache["val"]
        val: tuple[float | None, str | None] = (None, None)
        try:
            from odooguard.pg import PgBackRest
            info = PgBackRest(cfg).info()
            val = (info.backups[0].stop if info.backups else None, None)
        except Exception as exc:  # noqa: BLE001
            val = (None, str(exc).splitlines()[0][:300])
        _window_cache.update(ts=time.time(), val=val)
        return val

    def targets() -> list[dict]:
        c = fresh_cfg()
        out = [{"name": "odoo", "label": f"server01 - PRODUCTION ({c.source.host})", "prod": True}]
        out += [{"name": n, "label": f"{t.label or n} ({t.host})", "prod": t.is_production}
                for n, t in c.targets.items()]
        return out

    def form_params(form: dict) -> dict:
        when = None
        if form.get("moment") == "instant":
            raw = (form.get("when") or "").strip()
            if not raw:
                raise OdooGuardError("indiquer la date et l'heure à restaurer", "usage")
            when = dt.datetime.fromisoformat(raw).astimezone().isoformat()
        replace = form.get("mode") == "replace"
        as_name = (form.get("db") or "") if replace else (form.get("as_name") or "").strip()
        if not as_name:
            stamp = dt.datetime.fromisoformat(when).strftime("%Y%m%d_%H%M") if when else \
                time.strftime("%Y%m%d_%H%M")
            as_name = f"{form.get('db')}_copie_{stamp}"
        return {"db": form.get("db", ""), "as_name": as_name, "target": form.get("target", ""),
                "when": when, "replace": replace, "neutralize": "auto",
                "with_filestore": form.get("with_filestore") == "on",
                "confirm": (form.get("confirm") or "").strip() or None}

    @app.get("/restore", response_class=HTMLResponse)
    def restore_form(request: Request):
        user, role, err = require(request, "operateur")
        if err:
            return err
        start, werr = pitr_window()
        return page(request, "restore.html", user=user, databases=cfg.databases, targets=targets(),
                    window_start=start, window_error=werr, form={}, error=None,
                    recent=history.jobs_recent(10))

    @app.post("/restore/preview", response_class=HTMLResponse)
    async def restore_preview(request: Request):
        form = dict(await request.form())
        user, role, err = require(request, "operateur", form.get("csrf", ""))
        if err:
            return err
        start, werr = pitr_window()
        try:
            params = form_params(form)
            params["confirm"] = params["as_name"] if params["replace"] else None  # validé à l'étape suivante
            v = jobs.validate_restore_params(cfg, role, params)
            if v["when"] and start and v["when"].timestamp() < start:
                raise OdooGuardError(f"instant antérieur à la plus ancienne sauvegarde ({fmt_time(start)})", "usage")
            from odooguard.remote import Agent
            exists = Agent(cfg.target(v["target"])).call_json("db-exists", v["as_name"]).get("exists", False)
            if exists and not v["replace"]:
                raise OdooGuardError(f"la base « {v['as_name']} » existe déjà sur la cible : choisir un autre nom", "usage")
        except (OdooGuardError, ValueError) as exc:
            return page(request, "restore.html", user=user, databases=cfg.databases, targets=targets(),
                        window_start=start, window_error=werr, form=form, error=str(exc),
                        recent=history.jobs_recent(10))
        target = cfg.target(v["target"])
        neutral = v["neutralize"] if v["neutralize"] is not None else not (target.is_production and v["as_name"] == v["db"])
        return page(request, "restore_preview.html", user=user, v=v, target=target, exists=exists,
                    neutral=neutral, params=json.dumps({**params, "confirm": None}))

    @app.post("/restore/submit")
    async def restore_submit(request: Request):
        form = dict(await request.form())
        user, role, err = require(request, "operateur", form.get("csrf", ""))
        if err:
            return err
        try:
            params = json.loads(form.get("params", "{}"))
            params["confirm"] = (form.get("confirm") or "").strip() or None
            jobs.validate_restore_params(cfg, role, params)
        except (OdooGuardError, ValueError) as exc:
            return page(request, "error.html", user=user, message=str(exc))
        job_id = history.job_create(user, role, "restore-db", params)
        history.audit("web-restore-request", f"{params['target']}:{params['as_name']}", "queued",
                      {"job": job_id, "user": user, "db": params["db"], "when": params["when"],
                       "replace": params["replace"], "ip": client_ip(request)})
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/restore-tests/run")
    async def restore_test_run(request: Request):
        form = dict(await request.form())
        user, role, err = require(request, "operateur", form.get("csrf", ""))
        if err:
            return err
        job_id = history.job_create(user, role, "restore-test", {})
        history.audit("web-restore-test-request", cfg.restore_test.target, "queued",
                      {"job": job_id, "ip": client_ip(request)})
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    def job_view(job_id: int) -> dict | None:
        job = history.job_get(job_id)
        if not job:
            return None
        job["params_d"] = json.loads(job["params"] or "{}")
        job["result_d"] = json.loads(job["result"]) if job.get("result") else None
        tail = ""
        if job.get("log_path") and os.path.exists(job["log_path"]):
            with open(job["log_path"], encoding="utf-8", errors="replace") as fh:
                tail = "".join(fh.readlines()[-80:])
        job["log_tail"] = tail
        return job

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: int):
        user, role, err = require(request, "lecteur")
        if err:
            return err
        job = job_view(job_id)
        if not job:
            return page(request, "error.html", user=user, message="Travail introuvable.")
        return page(request, "job.html", user=user, job=job)

    @app.get("/jobs/{job_id}/partial", response_class=HTMLResponse)
    def job_partial(request: Request, job_id: int):
        user, role, err = require(request, "lecteur")
        if err:
            return err
        return page(request, "_job.html", user=user, job=job_view(job_id))

    def reports(limit: int = 30) -> list[dict]:
        out = []
        for path in sorted(glob.glob(os.path.join(cfg.state_dir, "reports", "restore-test-*.json")),
                           reverse=True)[:limit]:
            try:
                with open(path, encoding="utf-8") as fh:
                    r = json.load(fh)
                r["id"] = os.path.basename(path)[len("restore-test-"):-len(".json")]
                out.append(r)
            except (OSError, ValueError):
                continue
        return out

    @app.get("/restore-tests", response_class=HTMLResponse)
    def restore_tests(request: Request):
        user, role, err = require(request, "lecteur")
        if err:
            return err
        return page(request, "restore_tests.html", user=user, reports=reports(),
                    settings=cfg.restore_test)

    @app.get("/restore-tests/{rid}", response_class=HTMLResponse)
    def restore_test_detail(request: Request, rid: str):
        user, role, err = require(request, "lecteur")
        if err:
            return err
        rep = next((r for r in reports(500) if r["id"] == rid), None)
        if rep is None:
            return page(request, "error.html", user=user, message="Rapport introuvable.")
        from odooguard.restoretest import format_report
        return page(request, "restore_test_detail.html", user=user, r=rep, text=format_report(rep))

    # ------------------------------------------------------------ NAS (gestion graphique)
    def fresh_cfg() -> Config:
        """Configuration relue à chaque affichage : odooguard-jobs la modifie."""
        from odooguard import config as config_mod
        try:
            return config_mod.load(cfg.path)
        except Exception:  # noqa: BLE001
            return cfg
    def nas_rows() -> list[dict]:
        st = {c["name"]: c for c in load_status().get("components", [])}
        rows = []
        for d in fresh_cfg().destinations.values():
            rows.append({"d": d, "access": st.get(f"nas:{d.name}"), "copy_state": st.get(f"copy:{d.name}"),
                         "managed": bool(d.share)})
        return rows

    @app.get("/nas", response_class=HTMLResponse)
    def nas_list(request: Request):
        user, role, err = require(request, "lecteur")
        if err:
            return err
        return page(request, "nas.html", user=user, rows=nas_rows(), recent=history.jobs_recent(5))

    def nas_form_page(request, user, form, error=None, is_new=True):
        return page(request, "nas_form.html", user=user, form=form, error=error, is_new=is_new,
                    versions=("3.1.1", "3.0", "2.1", "2.0"))

    @app.get("/nas/new", response_class=HTMLResponse)
    def nas_new(request: Request):
        user, role, err = require(request, "admin")
        if err:
            return err
        used = set(fresh_cfg().destinations)
        name = next(f"nas{i}" for i in range(1, 100) if f"nas{i}" not in used)
        return nas_form_page(request, user, {"name": name, "subdir": "odooguard", "smb_version": "3.0",
                                             "min_free_gb": "500", "enabled": True})

    @app.get("/nas/{name}/edit", response_class=HTMLResponse)
    def nas_edit(request: Request, name: str):
        user, role, err = require(request, "admin")
        if err:
            return err
        d = fresh_cfg().destinations.get(name)
        if d is None:
            return page(request, "error.html", user=user, message="NAS inconnu.")
        # NAS de la configuration d'origine, jamais encore monté par OdooGuard :
        # on propose le partage documenté (docs/installation-phase1.md).
        known = {"/mnt/nas-odoo": "//192.168.8.30/odoo", "/mnt/nas-backups": "//192.168.3.19/Backups"}
        share = d.share or known.get(d.mountpoint, "")
        return nas_form_page(request, user, {"name": d.name, "share": share, "username": d.username,
                                             "domain": d.domain, "smb_version": d.smb_version,
                                             "subdir": d.subdir, "min_free_gb": f"{d.min_free_gb:g}",
                                             "enabled": d.enabled, "managed": bool(d.share)},
                             is_new=not d.share)

    @app.post("/nas/save")
    async def nas_save(request: Request):
        form = dict(await request.form())
        user, role, err = require(request, "admin", form.get("csrf", ""))
        if err:
            return err
        from odooguard import nas
        password = form.get("password") or ""
        is_new = not (form.get("name") in fresh_cfg().destinations and fresh_cfg().destinations[form["name"]].share)
        params = {k: form.get(k, "") for k in ("name", "share", "username", "domain", "smb_version",
                                               "subdir", "min_free_gb")}
        params["enabled"] = form.get("enabled") == "on"
        params["has_password"] = bool(password)
        try:
            v = nas.validate(params, require_password=is_new)
        except OdooGuardError as exc:
            shown = {**params, "enabled": params["enabled"]}
            return nas_form_page(request, user, shown, error=str(exc), is_new=is_new)
        job_params = {**v, "has_password": bool(password)}
        if password:
            job_params["secret_token"] = nas.stash_password(cfg, password)
        job_id = history.job_create(user, role, "nas-apply", job_params)
        history.audit("web-nas-request", f"nas:{v['name']}", "queued",
                      {"job": job_id, "share": v["share"], "user": v["username"],
                       "password_changed": bool(password), "ip": client_ip(request)})
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/nas/{name}/test")
    async def nas_test(request: Request, name: str):
        form = dict(await request.form())
        user, role, err = require(request, "operateur", form.get("csrf", ""))
        if err:
            return err
        if name not in fresh_cfg().destinations:
            return page(request, "error.html", user=user, message="NAS inconnu.")
        job_id = history.job_create(user, role, "nas-test", {"name": name})
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/nas/{name}/enable")
    async def nas_enable(request: Request, name: str):
        form = dict(await request.form())
        user, role, err = require(request, "admin", form.get("csrf", ""))
        if err:
            return err
        if name not in fresh_cfg().destinations:
            return page(request, "error.html", user=user, message="NAS inconnu.")
        return queue(request, user, role, "nas-enable", {"name": name, "enabled": form.get("enabled") == "1"},
                     target=f"nas:{name}")

    @app.post("/nas/{name}/remove")
    async def nas_remove(request: Request, name: str):
        form = dict(await request.form())
        user, role, err = require(request, "admin", form.get("csrf", ""))
        if err:
            return err
        if name not in fresh_cfg().destinations:
            return page(request, "error.html", user=user, message="NAS inconnu.")
        if (form.get("confirm") or "").strip() != name:
            return page(request, "error.html", user=user,
                        message=f"Retrait annulé : il fallait retaper exactement « {name} ».")
        job_id = history.job_create(user, role, "nas-remove", {"name": name, "confirm": name})
        history.audit("web-nas-remove-request", f"nas:{name}", "queued",
                      {"job": job_id, "ip": client_ip(request)})
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    # ------------------------------------------------------------ Réglages (admin)
    def queue(request, user, role, kind, params, target="") -> RedirectResponse:
        job_id = history.job_create(user, role, kind, params)
        safe = {k: v for k, v in params.items() if not k.endswith("_token") and k != "confirm"}
        history.audit(f"web-{kind}-request", target or kind, "queued",
                      {"job": job_id, "ip": client_ip(request), **safe})
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/reglages", response_class=HTMLResponse)
    def reglages(request: Request):
        user, role, err = require(request, "lecteur")
        if err:
            return err
        return page(request, "reglages.html", user=user)

    # ---- planification
    @app.get("/planification", response_class=HTMLResponse)
    def planification(request: Request):
        user, role, err = require(request, "lecteur")
        if err:
            return err
        from odooguard import schedule
        return page(request, "planification.html", user=user, timers=schedule.all_timers(),
                    tz=schedule.current_timezone(), zones=schedule.timezones())

    @app.post("/planification/timezone")
    async def planification_timezone(request: Request):
        form = dict(await request.form())
        user, role, err = require(request, "admin", form.get("csrf", ""))
        if err:
            return err
        from odooguard import schedule
        try:
            tz = schedule.validate_timezone(form.get("timezone", ""))
        except OdooGuardError as exc:
            return page(request, "error.html", user=user, message=str(exc))
        return queue(request, user, role, "schedule", {"action": "timezone", "timezone": tz},
                     target="timezone")

    @app.get("/planification/{timer}", response_class=HTMLResponse)
    def planification_form(request: Request, timer: str):
        user, role, err = require(request, "admin")
        if err:
            return err
        from odooguard import schedule
        if timer not in schedule.TIMERS:
            return page(request, "error.html", user=user, message="Minuteur inconnu.")
        t = schedule.current(timer)
        return page(request, "planification_form.html", user=user, t=t, days=schedule.DAYS,
                    tz=schedule.current_timezone(),
                    intervals=list(schedule.INTERVALS), form={"mode": "advanced",
                                                             "advanced": "\n".join(t["specs"])},
                    error=None)

    @app.post("/planification/save")
    async def planification_save(request: Request):
        form = dict(await request.form())
        user, role, err = require(request, "admin", form.get("csrf", ""))
        if err:
            return err
        from odooguard import schedule
        timer = form.get("timer", "")
        if timer not in schedule.TIMERS:
            return page(request, "error.html", user=user, message="Minuteur inconnu.")
        try:
            specs = schedule.validate_specs(schedule.build_specs(form))
        except OdooGuardError as exc:
            return page(request, "planification_form.html", user=user, t=schedule.current(timer),
                        days=schedule.DAYS, intervals=list(schedule.INTERVALS), form=form, error=str(exc),
                        tz=schedule.current_timezone())
        return queue(request, user, role, "schedule",
                     {"timer": timer, "action": "set", "specs": specs, "enable": form.get("enable") == "on"},
                     target=timer)

    @app.post("/planification/{timer}/{action}")
    async def planification_action(request: Request, timer: str, action: str):
        form = dict(await request.form())
        user, role, err = require(request, "admin", form.get("csrf", ""))
        if err:
            return err
        from odooguard import schedule
        if timer not in schedule.TIMERS or action not in ("enable", "disable", "reset"):
            return page(request, "error.html", user=user, message="Action inconnue.")
        return queue(request, user, role, "schedule", {"timer": timer, "action": action}, target=timer)

    # ---- notifications
    def secret_set(path: str) -> bool:
        try:
            return os.path.getsize(path) > 0
        except OSError:
            return False

    @app.get("/notifications", response_class=HTMLResponse)
    def notifications(request: Request):
        user, role, err = require(request, "admin")
        if err:
            return err
        c = fresh_cfg()
        a = c.alerts
        from odooguard.config import notify_categories
        return page(request, "notifications.html", user=user, a=a, error=None, form=None,
                    categories=notify_categories(c),
                    smtp_pw_set=secret_set(a.smtp_password_file),
                    tg_token_set=secret_set(a.telegram_token_file))

    @app.post("/notifications/save")
    async def notifications_save(request: Request):
        form = dict(await request.form())
        user, role, err = require(request, "admin", form.get("csrf", ""))
        if err:
            return err
        from odooguard import settings
        params = {k: form.get(k, "") for k in ("min_level", "repeat_hours", "smtp_host", "smtp_port",
                                               "smtp_security", "smtp_user", "mail_from", "mail_to",
                                               "telegram_chat_ids", "https_proxy")}
        for flag in ("use_email", "use_telegram", "send_resolved"):
            params[flag] = form.get(flag) == "on"
        from odooguard.config import notify_categories
        cats = notify_categories(fresh_cfg())
        params["routes_present"] = form.get("routes_present") == "1"
        for cat in cats:
            for ch in ("email", "telegram"):
                params[f"route_{cat}_{ch}"] = form.get(f"route_{cat}_{ch}") == "on"
        a = fresh_cfg().alerts
        try:
            settings.validate_notifications({**params, "route_categories": list(cats)})
            if form.get("smtp_password"):
                params["smtp_password_token"] = settings.stash_secret(cfg, form["smtp_password"])
            if form.get("telegram_token"):
                params["telegram_token_token"] = settings.stash_secret(cfg, form["telegram_token"].strip())
        except OdooGuardError as exc:
            return page(request, "notifications.html", user=user, a=a, error=str(exc), form=form,
                        categories=cats,
                        smtp_pw_set=secret_set(a.smtp_password_file),
                        tg_token_set=secret_set(a.telegram_token_file))
        return queue(request, user, role, "notify-save", params, target="notifications")

    @app.post("/notifications/test")
    async def notifications_test(request: Request):
        form = dict(await request.form())
        user, role, err = require(request, "admin", form.get("csrf", ""))
        if err:
            return err
        channel = form.get("channel", "")
        if channel not in ("", "email", "telegram"):
            return page(request, "error.html", user=user, message="Canal inconnu.")
        return queue(request, user, role, "notify-test", {"channel": channel},
                     target=f"notifications:{channel or 'tous'}")

    @app.post("/notifications/telegram")
    async def notifications_telegram(request: Request):
        form = dict(await request.form())
        user, role, err = require(request, "admin", form.get("csrf", ""))
        if err:
            return err
        from odooguard import settings
        params = {}
        if form.get("telegram_token"):
            params["token_token"] = settings.stash_secret(cfg, form["telegram_token"].strip())
        return queue(request, user, role, "telegram-discover", params, target="telegram")

    # ---- bases Odoo suivies
    def known_databases(c: Config) -> tuple[list[str], str | None]:
        from odooguard.pg import PgBackRest
        try:
            return PgBackRest(c).database_list(), None
        except Exception as exc:  # noqa: BLE001
            return [], str(exc)[:300]

    @app.get("/bases", response_class=HTMLResponse)
    def bases(request: Request):
        user, role, err = require(request, "lecteur")
        if err:
            return err
        c = fresh_cfg()
        found, found_err = known_databases(c)
        return page(request, "bases.html", user=user, current=c.databases, found=found,
                    found_err=found_err, error=None)

    @app.post("/bases/save")
    async def bases_save(request: Request):
        form = await request.form()
        user, role, err = require(request, "admin", form.get("csrf", ""))
        if err:
            return err
        from odooguard import settings
        names = list(form.getlist("db")) + settings._split(form.get("other", ""))
        try:
            dbs = settings.validate_databases(names)
        except OdooGuardError as exc:
            c = fresh_cfg()
            found, found_err = known_databases(c)
            return page(request, "bases.html", user=user, current=c.databases, found=found,
                        found_err=found_err, error=str(exc))
        return queue(request, user, role, "databases-save", {"databases": dbs}, target="bases")

    # ---- rétention
    def retention_ctx(c: Config) -> dict:
        from odooguard import schedule
        from odooguard.pg import PgBackRest
        eff = PgBackRest(c).retention_effective()
        try:
            full_plan = " ; ".join(schedule.current("odooguard-backup-db-full.timer")["specs"])
        except Exception:  # noqa: BLE001
            full_plan = ""
        return {"full": eff["full"] or 4, "diff": eff["diff"] or 7, "keep": c.restic_keep, "full_plan": full_plan}

    @app.get("/retention", response_class=HTMLResponse)
    def retention(request: Request):
        user, role, err = require(request, "lecteur")
        if err:
            return err
        return page(request, "retention.html", user=user, r=retention_ctx(fresh_cfg()), form=None, error=None)

    @app.post("/retention/save")
    async def retention_save(request: Request):
        form = dict(await request.form())
        user, role, err = require(request, "admin", form.get("csrf", ""))
        if err:
            return err
        from odooguard import settings
        params = {k: form.get(k, "") for k in ("retention_full", "retention_diff", "keep_hourly", "keep_daily",
                                               "keep_weekly", "keep_monthly")}
        try:
            settings.validate_retention(params)
        except OdooGuardError as exc:
            return page(request, "retention.html", user=user, r=retention_ctx(fresh_cfg()), form=form,
                        error=str(exc))
        return queue(request, user, role, "retention-save", params, target="retention")

    # ---- VM de test
    TESTVM_FIELDS = ("label", "host", "odoo_url", "pg_version", "pg_cluster", "odoo_service", "odoo_conf",
                     "filestore_path")

    def testvm_values(c: Config) -> dict:
        t = c.targets.get(c.restore_test.target)
        if t is None:
            return {"label": "VM de test", "host": "", "odoo_url": "", "pg_version": c.source.pg_version,
                    "pg_cluster": "main", "odoo_service": "odoo", "odoo_conf": "/etc/odoo/odoo.conf",
                    "filestore_path": c.source.filestore_path}
        return {"label": t.label or "VM de test", **{k: getattr(t, k) for k in TESTVM_FIELDS[1:]}}

    @app.get("/vm-test", response_class=HTMLResponse)
    def vm_test(request: Request):
        user, role, err = require(request, "lecteur")
        if err:
            return err
        c = fresh_cfg()
        return page(request, "vm_test.html", user=user, v=testvm_values(c), target_name=c.restore_test.target,
                    prod_host=c.source.host, error=None)

    @app.post("/vm-test/save")
    async def vm_test_save(request: Request):
        form = dict(await request.form())
        user, role, err = require(request, "admin", form.get("csrf", ""))
        if err:
            return err
        from odooguard import settings
        c = fresh_cfg()
        params = {k: form.get(k, "") for k in TESTVM_FIELDS}
        try:
            settings.validate_testvm(c, params)
        except OdooGuardError as exc:
            return page(request, "vm_test.html", user=user, v=params, target_name=c.restore_test.target,
                        prod_host=c.source.host, error=str(exc))
        return queue(request, user, role, "testvm-save", params, target="vm-test")

    @app.post("/vm-test/test")
    async def vm_test_test(request: Request):
        form = dict(await request.form())
        user, role, err = require(request, "admin", form.get("csrf", ""))
        if err:
            return err
        return queue(request, user, role, "testvm-test", {}, target="vm-test")

    # ---- surveillance (seuils, services, sécurité)
    MON_FIELDS = ("disk_warn_pct", "disk_crit_pct", "services_local", "failed_logins_warn",
                  "failed_logins_window", "keep_days")

    @app.get("/surveillance", response_class=HTMLResponse)
    def surveillance(request: Request):
        user, role, err = require(request, "lecteur")
        if err:
            return err
        c = fresh_cfg()
        return page(request, "surveillance.html", user=user, m=c.monitoring, form=None, error=None,
                    thresholds=threshold_rows(c), tform=None, externals=c.externals)

    @app.post("/surveillance/save")
    async def surveillance_save(request: Request):
        form = dict(await request.form())
        user, role, err = require(request, "admin", form.get("csrf", ""))
        if err:
            return err
        from odooguard import settings
        params = {k: form.get(k, "") for k in MON_FIELDS}
        params["security"] = form.get("security") == "on"
        params["notify_accounts"] = form.get("notify_accounts") == "on"
        try:
            v = settings.validate_monitoring(params)
        except OdooGuardError as exc:
            c = fresh_cfg()
            return page(request, "surveillance.html", user=user, m=c.monitoring, form=form,
                        error=str(exc), thresholds=threshold_rows(c), tform=None, externals=c.externals)
        v["services_local"] = ", ".join(v["services_local"])
        return queue(request, user, role, "monitoring-save", v, target="surveillance")

    def threshold_rows(c: Config) -> list[dict]:
        from odooguard import schedule, settings
        cur = {"wal_max_lag": c.max_age["wal"], "db_any_max_age": c.max_age["db_any"],
               "db_full_max_age": c.max_age["db_full"], "files_max_age": c.max_age["files"],
               "copy_max_age": c.max_age["copy"], "restore_test_max_age": c.restore_test.max_age}
        timer = {"db_any_max_age": "odooguard-backup-db-incr.timer", "db_full_max_age": "odooguard-backup-db-full.timer",
                 "files_max_age": "odooguard-backup-files.timer", "copy_max_age": "odooguard-copy.timer",
                 "restore_test_max_age": "odooguard-restore-test.timer"}
        rows = []
        for key, (lib, unit, lo, hi) in settings.THRESHOLDS.items():
            plan = None
            if key in timer:
                try:
                    t = schedule.current(timer[key])
                    plan = ("suspendue" if not t["enabled"] else " ; ".join(t["specs"])) if t["installed"] else None
                except Exception:  # noqa: BLE001
                    plan = None
            rows.append({"key": key, "label": lib, "value": f"{cur[key] / unit:g}", "min": lo, "max": hi,
                         "plan": plan})
        return rows

    @app.post("/surveillance/externes")
    async def surveillance_externes(request: Request):
        form = await request.form()
        user, role, err = require(request, "admin", form.get("csrf", ""))
        if err:
            return err
        names = [n for n in form.getlist("ext") if n in fresh_cfg().externals]
        return queue(request, user, role, "externals-save", {"enabled": names}, target="externes")

    @app.post("/surveillance/seuils")
    async def surveillance_seuils(request: Request):
        form = dict(await request.form())
        user, role, err = require(request, "admin", form.get("csrf", ""))
        if err:
            return err
        from odooguard import settings
        params = {k: form.get(k, "") for k in settings.THRESHOLDS}
        try:
            settings.validate_thresholds(params)
        except OdooGuardError as exc:
            c = fresh_cfg()
            return page(request, "surveillance.html", user=user, m=c.monitoring, form=None, error=str(exc),
                        thresholds=threshold_rows(c), tform=form, externals=c.externals)
        return queue(request, user, role, "thresholds-save", params, target="seuils")

    # ---- sécurité Odoo (lecture)
    @app.get("/securite", response_class=HTMLResponse)
    def securite(request: Request, days: int = 7):
        user, role, err = require(request, "lecteur")
        if err:
            return err
        days = days if days in (1, 7, 30, 90) else 7
        now = time.time()
        since = now - days * 86400
        c = fresh_cfg()
        users = {}
        for db in c.databases:
            snap = history.kv_get(f"odoo_users:{db}") or {}
            rows = sorted(snap.values(), key=lambda u: u.get("created") or 0, reverse=True)
            users[db] = {"total": len(rows),
                         "internal": sum(1 for u in rows if u.get("active") and not u.get("share")),
                         "portal": sum(1 for u in rows if u.get("active") and u.get("share")),
                         "recent": rows[:15]}
        window = c.monitoring.failed_logins_window
        return page(request, "securite.html", user=user, days=days, m=c.monitoring,
                    failed=history.security_events(since, ("login_failed",), 500),
                    succeeded=history.security_events(since, ("login_ok",), 100),
                    accounts=history.security_events(since, tuple(ACCOUNT_KINDS), 300),
                    top=history.security_top_ips(since),
                    n_failed=history.security_count(since, "login_failed"),
                    n_ok=history.security_count(since, "login_ok"),
                    n_window=history.security_count(now - window * 60, "login_failed"),
                    users=users, source=history.kv_get("odoo_auth_source", ""),
                    cursor=history.kv_get("odoo_auth_cursor"), account_fr=ACCOUNT_KINDS)

    # ---- comptes
    @app.get("/utilisateurs", response_class=HTMLResponse)
    def utilisateurs(request: Request):
        user, role, err = require(request, "admin")
        if err:
            return err
        return page(request, "utilisateurs.html", user=user, accounts=auth.read_accounts(cfg.web.users_file),
                    roles=auth.ROLES, error=None)

    @app.post("/utilisateurs/save")
    async def utilisateurs_save(request: Request):
        form = dict(await request.form())
        user, role, err = require(request, "admin", form.get("csrf", ""))
        if err:
            return err
        from odooguard import settings
        name, new_role = (form.get("name") or "").strip(), form.get("role", "")
        pw, pw2 = form.get("password") or "", form.get("password2") or ""
        accounts = auth.read_accounts(cfg.web.users_file)
        error = None
        if not auth.valid_username(name):
            error = "nom invalide (2 à 32 caractères : lettres, chiffres, . _ -)"
        elif new_role not in auth.ROLES:
            error = "rôle invalide"
        elif name not in accounts and not pw:
            error = "mot de passe obligatoire pour un nouveau compte"
        elif pw and len(pw) < 12:
            error = "mot de passe trop court (12 caractères minimum)"
        elif pw != pw2:
            error = "les deux saisies du mot de passe diffèrent"
        if error:
            return page(request, "utilisateurs.html", user=user, accounts=accounts, roles=auth.ROLES,
                        error=error)
        params = {"name": name, "role": new_role}
        if pw:
            params["hash_token"] = settings.stash_secret(cfg, auth.hash_password(pw))
        return queue(request, user, role, "user-save", params, target=f"user:{name}")

    @app.post("/utilisateurs/{name}/delete")
    async def utilisateurs_delete(request: Request, name: str):
        form = dict(await request.form())
        user, role, err = require(request, "admin", form.get("csrf", ""))
        if err:
            return err
        if (form.get("confirm") or "").strip() != name:
            return page(request, "error.html", user=user,
                        message=f"Suppression annulée : il fallait retaper exactement « {name} ».")
        return queue(request, user, role, "user-delete", {"name": name, "confirm": name},
                     target=f"user:{name}")

    return app


def serve(cfg: Config) -> None:  # pragma: no cover - lancé par systemd
    import uvicorn
    ssl_args = {}
    if os.path.exists(cfg.web.certfile) and os.path.exists(cfg.web.keyfile):
        ssl_args = {"ssl_certfile": cfg.web.certfile, "ssl_keyfile": cfg.web.keyfile}
    else:
        log.warning("certificat absent : tableau de bord en HTTP (à éviter)")
    uvicorn.run(create_app(cfg), host=cfg.web.listen, port=cfg.web.port, proxy_headers=False,
                server_header=False, log_level="info", **ssl_args)
