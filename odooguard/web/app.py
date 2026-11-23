"""Application web OdooGuard (phase 2 : consultation ; phase 3 : assistant de restauration).

Lancement (service ``odooguard-web``) :
    odooguard web          # uvicorn en HTTPS sur [web] listen:port

Le tableau de bord LIT l'état calculé toutes les 5 min par ``odooguard status
--record`` (status.json) et l'historique SQLite : il ne lance aucune
opération et peut s'arrêter sans affecter les sauvegardes (systemd timers).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from odooguard import __version__, external
from odooguard.config import Config
from odooguard.history import History
from odooguard.util import fmt_age, fmt_size, fmt_time
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


def create_app(cfg: Config, history: History | None = None) -> FastAPI:
    history = history or History(cfg.history_db)
    secret = auth.load_secret(cfg.web.secret_file)
    throttle = auth.Throttle()
    app = FastAPI(title="OdooGuard", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals.update(fmt_age=fmt_age, fmt_size=fmt_size, fmt_time=fmt_time,
                                 LEVEL_FR=LEVEL_FR, LEVEL_ICON=LEVEL_ICON, site=cfg.site,
                                 version=__version__, label=component_label)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # ------------------------------------------------------------ outils
    def current_user(request: Request) -> str | None:
        user = auth.read_session(secret, request.cookies.get(auth.COOKIE))
        if user and user in auth.read_users(cfg.web.users_file):
            return user
        return None

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
        return templates.TemplateResponse(
            request, name, {"user": ctx.pop("user", None), "now_ts": time.time(), **ctx})

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
            files = list(reversed(Restic(cfg).snapshots()))[:100]
        except Exception as exc:  # noqa: BLE001
            errors.append(f"restic : {exc}")
        return page(request, "backups.html", user=user, db=db, files=files, errors=errors,
                    window=window)

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
