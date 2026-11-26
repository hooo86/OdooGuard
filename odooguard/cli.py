"""Interface en ligne de commande : ``odooguard backup|restore|list|verify|status``.

Codes de retour : 0 succès / état OK, 1 avertissement, 2 échec / critique,
3 erreur d'utilisation ou de configuration.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import json
import logging
import os
import pwd
import re
import signal
import sys
import threading
import time

from odooguard import __version__, notify
from odooguard import config as config_mod
from odooguard import dest as dest_mod
from odooguard import status as status_mod
from odooguard.audit import Auditor, confirm_destructive
from odooguard.files import Restic, pull_mirror
from odooguard.history import History
from odooguard.pg import PgBackRest, WalIngest
from odooguard.progress import Progress
from odooguard.restore import Restorer
from odooguard.util import (LockBusy, OdooGuardError, check_free_space, file_lock, fmt_age,
                            fmt_size, fmt_time, io_probe, parse_time, run)

log = logging.getLogger("odooguard")


# ====================================================================== outils
class Ctx:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.cfg = config_mod.load(args.config)
        os.makedirs(self.cfg.state_dir, exist_ok=True)
        self.history = History(self.cfg.history_db)
        self.auditor = Auditor(self.history, self.cfg.audit_log)
        notify.configure(self.cfg, self.history)


_KIND_FR = {"full": "complète", "diff": "différentielle", "incr": "incrémentale"}
_STATS = re.compile(r"^(Number of|Total |Literal data|Matched data|File list|sent \d|total size|"
                    r"Total bytes)", re.I)


def what_fr(component: str, kind: str) -> str:
    if component == "db":
        return f"sauvegarde {_KIND_FR.get(kind, kind)} de la base"
    if component == "files":
        return "sauvegarde des fichiers (filestore, modules, configuration)"
    if component.startswith("copy:"):
        return f"copie vers {component[5:]}"
    return {"verify": "vérification des dépôts", "maintenance": "maintenance hebdomadaire",
            "restore": "restauration", "restore-test": "test de restauration"}.get(component, f"{component} ({kind})")


def failure_body(exc: Exception, category: str, max_lines: int = 12) -> str:
    """Message d'échec lisible : sans statistiques rsync ni lignes vides, tronqué."""
    lines = []
    # « rsync -a --numeric-ids ... -e 'ssh ...' source dest » : seul le nom de la commande sert
    text = re.sub(r"«\s*(\S+)[^»]{60,}»", r"« \1 … »", str(exc))
    for line in text.replace(" : rsync:", " :\nrsync:").splitlines():
        line = line.strip()
        if line and not _STATS.match(line):
            lines.append(line[:400])
    text = "\n".join(lines[:max_lines]) or str(exc)[:600]
    return f"{text}\n\nCatégorie : {category}. Détail : tableau de bord, page Historique."


def tracked(ctx: Ctx, component: str, kind: str, func, *, alert: bool = True):
    """Exécute ``func`` en traçant début/fin dans l'historique.

    En cas d'échec : statut « failed » avec sa catégorie (io, space, mount...),
    alerte immédiate et code retour non nul (=> OnFailure systemd).
    """
    ctx.history.mark_interrupted()
    run_id = ctx.history.start(component, kind)
    t0 = time.monotonic()
    try:
        result = func() or {}
    except KeyboardInterrupt:
        # Arrêt demandé (Ctrl+C, systemctl stop) : fin enregistrée, sans alerte critique
        ctx.history.finish(run_id, "failed", message="exécution interrompue (arrêt demandé)",
                           category="interrupted")
        raise
    except Exception as exc:  # noqa: BLE001 - on veut tout tracer
        cat = getattr(exc, "category", "error")
        partial = getattr(exc, "result", None) or {}  # échec partiel : ce qui a été fait malgré tout
        ctx.history.finish(run_id, "failed", message=str(exc), category=cat, size=partial.get("size"),
                           added=partial.get("added"), label=partial.get("label"))
        if alert:
            notify.send("critical", f"échec : {what_fr(component, kind)}", failure_body(exc, cat),
                        category=notify.category_of(component) if component.startswith("copy:") else "echec")
        raise
    ctx.history.finish(run_id, "success", size=result.get("size"), added=result.get("added"),
                       label=result.get("label"), message=result.get("message", ""),
                       details=result)
    log.info("%s (%s) terminé en %.0f s", component, kind, time.monotonic() - t0)
    return result


def preflight_local(ctx: Ctx) -> None:
    cfg = ctx.cfg
    check_free_space(cfg.repo_path, cfg.min_free_gb, "dépôt pgBackRest")
    io_probe(cfg.repo_path)


def out(ctx: Ctx, data, human: str) -> None:
    if getattr(ctx.args, "json", False):
        print(json.dumps(data, indent=2, default=str, ensure_ascii=False))
    else:
        print(human)


# ====================================================================== backup
_BACKUP_RANK = {"incr": 0, "diff": 1, "full": 2}


@contextmanager
def queued_lock(cfg, name: str, *, wait: float, holder: str, component: str, label: str):
    """Verrou avec file d'attente visible : tant qu'une autre opération le tient, une carte
    « en attente » apparaît dans « Sauvegardes en cours » au lieu d'une erreur."""
    card = {}

    def on_wait(h):
        if "p" not in card:
            card["p"] = Progress(cfg.state_dir, f"attente:{component}", f"{label} (en file d'attente)")
            card["p"].__enter__()
            card["t"] = 0.0
        if time.time() - card["t"] >= 30:
            since = f" depuis {fmt_age(time.time() - h['since'])}" if h.get("since") else ""
            card["p"].update(phase=f"en attente : {h.get('what') or 'autre opération'} en cours{since}",
                             force=True)
            card["t"] = time.time()
    try:
        with file_lock(cfg.lock_dir, name, wait=wait, holder=holder, on_wait=on_wait):
            if "p" in card:
                card.pop("p").close()
            yield
    finally:
        if "p" in card:
            card.pop("p").close()


def cmd_backup(ctx: Ctx) -> int:
    a = ctx.args
    cfg = ctx.cfg
    if a.what == "db":
        def job():
            preflight_local(ctx)
            pg = PgBackRest(cfg)
            kind_fr = {"full": "complète", "diff": "différentielle", "incr": "incrémentale"}[a.type]
            with Progress(cfg.state_dir, "db", f"Base PostgreSQL ({kind_fr})") as prog:
                prog.update(phase="point de contrôle PostgreSQL et préparation")
                stop = threading.Event()

                def poll():
                    # pgBackRest publie taille totale et taille copiée (mise à jour
                    # à chaque fichier terminé) : lue toutes les 10 s.
                    while not stop.wait(10):
                        try:
                            lk = pg.backup_progress()
                        except Exception:  # noqa: BLE001
                            continue
                        if lk and lk.get("size"):
                            prog.update(phase=f"copie des fichiers de la base ({kind_fr})",
                                        bytes_total=lk["size"], bytes_done=lk.get("size-cplt", 0))
                t = threading.Thread(target=poll, daemon=True)
                t.start()
                try:
                    pg.backup(a.type)
                finally:
                    stop.set()
                    t.join(timeout=15)
            last = pg.info().last()
            return {"label": last.label, "size": last.size, "added": last.repo_delta,
                    "duration": last.duration}
        try:
            with queued_lock(cfg, "db", wait=a.wait, holder=f"sauvegarde {a.type} de la base",
                             component=f"db-{a.type}", label=f"Base : sauvegarde {_KIND_FR.get(a.type, a.type)}"):
                r = tracked(ctx, "db", a.type, job)
        except LockBusy as exc:
            # Une sauvegarde de la base au moins aussi complète tourne déjà (ex. incrémentale de
            # 01:30 pendant la différentielle de 01:00) : celle-ci serait redondante -> ignorée,
            # sans alerte. Tout autre occupant du verrou reste une erreur, avec son nom.
            words = (exc.holder.get("what") or "").split()  # « sauvegarde diff de la base »
            running = _BACKUP_RANK.get(words[1], -1) if len(words) > 1 and words[-1] == "base" else -1
            if running >= _BACKUP_RANK[a.type]:
                log.info("sauvegarde %s ignorée : %s", a.type, exc)
                print(f"sauvegarde base {a.type} ignorée : {exc}")
                return 0
            raise
        print(f"sauvegarde base {a.type} OK : {r['label']} ({fmt_size(r['added'])} ajoutés)")
    elif a.what == "files":
        def job():
            check_free_space(cfg.mirror_dir, cfg.min_free_gb, "miroir")
            io_probe(cfg.restic_repo)
            from odooguard.remote import Agent
            Agent(cfg.source).call("export", timeout=600)  # pip freeze, paquets, versions
            with Progress(cfg.state_dir, "files", "Filestore, modules, configuration") as prog:
                stats = pull_mirror(cfg, progress=prog)
                prog.update(phase="2/2 instantané restic (dédupliqué, chiffré)")
                summ = Restic(cfg).backup(cfg.mirror_dir, host=cfg.source.host, tags=["files"],
                                          progress=prog)
            from odooguard.files import failed_sources
            failed = failed_sources(stats)
            if failed:
                ok = [s for s in stats if s not in failed]
                err = OdooGuardError(
                    f"instantané {summ['snapshot_id'][:8]} créé pour {', '.join(ok) or 'aucune source'} ; "
                    "EN ÉCHEC : " + " ; ".join(f"{s} ({e})" for s, e in failed.items()), "source")
                err.result = {"label": summ["snapshot_id"][:8], "size": summ.get("total_bytes_processed"),
                              "added": summ.get("data_added")}
                raise err
            return {"label": summ["snapshot_id"][:8], "size": summ.get("total_bytes_processed"),
                    "added": summ.get("data_added"), "rsync": stats}
        try:
            with queued_lock(cfg, "files", wait=a.wait, holder="sauvegarde des fichiers", component="files",
                             label="Fichiers (filestore, modules, configuration)"):
                r = tracked(ctx, "files", "snapshot", job)
        except LockBusy as exc:
            if exc.holder.get("what") == "sauvegarde des fichiers":
                log.info("sauvegarde des fichiers ignorée : la précédente n'est pas finie (%s)", exc)
                print(f"sauvegarde des fichiers ignorée : {exc}")
                return 0
            raise
        print(f"sauvegarde fichiers OK : instantané {r['label']} ({fmt_size(r['added'])} ajoutés)")
    elif a.what == "copy":
        names = [a.dest] if a.dest else [d.name for d in cfg.destinations.values() if d.enabled]
        failures = 0
        for name in names:
            # Configuration relue avant chaque NAS : une copie longue (plusieurs heures) ne doit
            # pas tenter un NAS suspendu ou retiré depuis son démarrage.
            try:
                current = config_mod.load(cfg.path).destinations.get(name)
            except config_mod.ConfigError:
                current = cfg.destinations.get(name)
            if not a.dest and (current is None or not current.enabled):
                log.info("copie vers %s ignorée : NAS suspendu ou retiré depuis le début de la copie", name)
                continue
            dest = current or cfg.destinations.get(name)
            if not dest:
                raise OdooGuardError(f"destination inconnue : {name}", "usage")

            def job(dest=dest):
                probe = dest_mod.preflight(dest)
                with Progress(cfg.state_dir, f"copy:{dest.name}", f"Copie vers {dest.name}") as prog:
                    # 1. Passe principale SANS verrou : longue, mais les sauvegardes de la base
                    #    et des fichiers continuent à l'heure prévue pendant ce temps.
                    dest_mod.copy_pgbackrest(cfg, dest, progress=prog, verify=False)
                    # 2. Passe finale sous le verrou « db » (rattrapage des derniers fichiers, quelques
                    #    minutes) : copie cohérente, contrôlée par pgBackRest.
                    prog.update(phase=f"finalisation et vérification de la copie de la base vers {dest.name}")
                    with file_lock(cfg.lock_dir, "db", wait=6 * 3600,
                                   holder=f"copie vers {dest.name} (passe finale)"):
                        pgr = dest_mod.copy_pgbackrest(cfg, dest, progress=prog)
                    # 3. restic gère lui-même la concurrence (verrous non exclusifs) ; le verrou
                    #    « files » écarte seulement la maintenance (suppression d'instantanés).
                    prog.update(phase=f"copie du dépôt fichiers (restic) vers {dest.name}")
                    with file_lock(cfg.lock_dir, "files", wait=6 * 3600,
                                   holder=f"copie restic vers {dest.name}"):
                        rr = dest_mod.copy_restic(cfg, dest)
                    prog.update(phase="écriture du manifeste de vérification")
                    # Référence pour la copie hors ligne G:\ (phase 3 bis) : liste le contenu du
                    # NAS lui-même (modifié seulement par cette copie), donc sans verrou
                    man = dest_mod.write_manifest(cfg, dest, pgr["label"], rr["snapshots"])
                return {"label": pgr["label"], "message": "copie vérifiée", "size": man["total_bytes"],
                        **probe, **rr, **man}
            try:
                tracked(ctx, f"copy:{name}", "copy", job)
                print(f"copie vers {name} OK")
            except OdooGuardError as exc:
                failures += 1
                print(f"copie vers {name} ÉCHEC : {exc}", file=sys.stderr)
        return 2 if failures else 0
    return 0


# ====================================================================== WAL
def cmd_wal_ingest(ctx: Ctx) -> int:
    ing = WalIngest(ctx.cfg)
    if ctx.args.refresh_shadow:
        ing.refresh_shadow()
        print("pg_control rafraîchi")
        return 0
    if ctx.args.once:
        print(f"{ing.run_once()} fichier(s) versé(s)")
        return 0
    if ctx.args.finalize_partial:
        from odooguard.pg import spool_state
        partial = spool_state(ctx.cfg)["partial"] or "-"
        confirm_destructive(
            action="finalisation du segment WAL en cours", expected=partial,
            force=ctx.args.force, confirm=ctx.args.confirm,
            description=("SINISTRE : à utiliser seulement si server01 est perdu ET le service "
                         "odooguard-walreceiver arrêté. Le segment en cours sera versé dans le "
                         "dépôt sous son nom définitif (dernières secondes de données)."))
        ctx.auditor.record("wal-finalize-partial", partial, "started")
        name = ing.finalize_partial()
        ctx.auditor.record("wal-finalize-partial", partial, "success", segment=name)
        print(f"segment {name} versé dans le dépôt")
        return 0
    last_alert = [0.0]

    def on_error(exc):
        if time.time() - last_alert[0] > 900:  # au plus une alerte / 15 min
            notify.send("critical", "échec du versement des WAL dans le dépôt",
                        failure_body(exc, getattr(exc, "category", "error")))
            last_alert[0] = time.time()
    ing.loop(on_error=on_error)
    return 0


# ====================================================================== list
def cmd_list(ctx: Ctx) -> int:
    cfg = ctx.cfg
    data: dict = {}
    lines: list[str] = []
    if ctx.args.what in ("db", "all"):
        info = PgBackRest(cfg).info()
        data["db"] = [b.__dict__ for b in info.backups]
        lines.append("== Base (pgBackRest, stanza %s) ==" % cfg.stanza)
        lines.append(f"{'étiquette':<36} {'type':<5} {'début':<19} {'durée':>7} "
                     f"{'taille':>10} {'ajouté':>10}")
        for b in info.backups:
            lines.append(f"{b.label:<36} {b.type:<5} {fmt_time(b.start):<19} "
                         f"{fmt_age(b.duration):>7} {fmt_size(b.size):>10} {fmt_size(b.repo_delta):>10}")
        if info.backups:
            lines.append(f"Restauration à un instant précis possible depuis "
                         f"{fmt_time(info.backups[0].stop)} jusqu'au dernier WAL reçu "
                         f"({info.wal_max}).")
    if ctx.args.what in ("files", "all"):
        snaps = Restic(cfg).snapshots()
        data["files"] = snaps
        lines.append("\n== Fichiers (restic) ==")
        for s in snaps[-ctx.args.limit:]:
            summ = s.get("summary") or {}
            lines.append(f"{s['short_id']}  {fmt_time(dt.datetime.fromisoformat(s['time'].replace('Z', '+00:00')))}"
                         f"  {fmt_size(summ.get('total_bytes_processed'))}"
                         f"  +{fmt_size(summ.get('data_added'))}")
    if ctx.args.what in ("runs", "all"):
        runs = ctx.history.recent(ctx.args.limit)
        data["runs"] = runs
        lines.append("\n== Dernières exécutions ==")
        for r in runs:
            dur = (r["ended_at"] or time.time()) - r["started_at"]
            lines.append(f"{fmt_time(r['started_at'])}  {r['component']:<14} {r['kind']:<8} "
                         f"{r['status']:<8} {fmt_age(dur):>8}  {(r['message'] or '')[:60]}")
    out(ctx, data, "\n".join(lines))
    return 0


# ====================================================================== verify
def cmd_verify(ctx: Ctx) -> int:
    cfg = ctx.cfg
    a = ctx.args

    def job():
        res = {}
        pg = PgBackRest(cfg)
        res["pgbackrest_check"] = "ok" if pg.check() else ""
        if a.level == "full":
            pg.verify()
            res["pgbackrest_verify"] = "ok"
        Restic(cfg).check(read_data_subset="100%" if a.level == "full" else "2%")
        res["restic_check"] = "ok"
        for d in cfg.destinations.values():
            if not d.enabled or (a.dest and d.name != a.dest):
                continue
            dest_mod.preflight(d)
            if a.level == "full":
                pg.verify(repo_path=os.path.join(d.path, "pgbackrest"))
                dest_mod.restic_for(cfg, d).check(read_data_subset="10%")
            res[f"dest:{d.name}"] = "ok"
        return {"message": json.dumps(res), **res}
    with file_lock(cfg.lock_dir, "verify", wait=0, holder="vérification des dépôts"):
        r = tracked(ctx, "verify", a.level, job)
    out(ctx, r, "\n".join(f"{k} : {v}" for k, v in r.items() if k != "message"))
    return 0


# ====================================================================== status
def cmd_status(ctx: Ctx) -> int:
    cfg = ctx.cfg
    if ctx.args.rpo_report:
        days = float(ctx.args.rpo_report.rstrip("dj"))
        rep = status_mod.rpo_report(ctx.history, days)
        human = (f"Perte de données maximale mesurée sur {days:g} j :\n"
                 f"  base     : {fmt_age(rep['db_seconds']['max'])} "
                 f"({rep['db_seconds']['samples']} mesures)\n"
                 f"  fichiers : {fmt_age(rep['files_seconds']['max'])} "
                 f"({rep['files_seconds']['samples']} mesures)")
        out(ctx, rep, human)
        return 0
    st = status_mod.compute(cfg, ctx.history, query_source=not ctx.args.no_remote)
    if ctx.args.record:
        status_mod.record(cfg, ctx.history, st)
    lines = [f"OdooGuard {cfg.site} - état global : {st['overall'].upper()}",
             f"Perte de données max. actuelle : base {fmt_age(st['rpo']['db_seconds'])}, "
             f"fichiers {fmt_age(st['rpo']['files_seconds'])}", ""]
    for c in st["components"]:
        extra = []
        if "age" in c:
            extra.append(f"âge {fmt_age(c['age'])}")
        if c.get("size") is not None:
            extra.append(f"taille {fmt_size(c['size'])}")
        if c.get("duration") is not None:
            extra.append(f"durée {fmt_age(c['duration'])}")
        lines.append(f"  [{c['level'].upper():^8}] {c['name']:<14} {', '.join(extra)}  {c.get('message', '')}")
    out(ctx, st, "\n".join(lines))
    if ctx.args.check:
        # Notification des seuls changements (nouveau, aggravé, rappel, résolu)
        notify.Alerter(cfg, ctx.history).process(st)
        return status_mod.LEVELS.get(st["overall"], 2)
    return 0


# ====================================================================== restore
def cmd_restore(ctx: Ctx) -> int:
    a = ctx.args
    cfg = ctx.cfg
    when = parse_time(a.time) if getattr(a, "time", None) else None
    r = Restorer(cfg, ctx.auditor)
    with file_lock(cfg.lock_dir, "restore", wait=0):
        if a.what == "db":
            neutralize = {"yes": True, "no": False, "auto": None}[a.neutralize]
            rep = tracked(ctx, "restore", "db", lambda: r.restore_db(
                db=a.db, as_name=a.as_name, when=when, target_name=a.target,
                neutralize=neutralize, with_filestore=not a.no_filestore, force=a.force,
                confirm=a.confirm, keep_staging=a.keep_staging), alert=False)
        elif a.what == "cluster":
            rep = tracked(ctx, "restore", "cluster", lambda: r.restore_cluster(
                target_name=a.target, when=when, force=a.force, confirm=a.confirm), alert=False)
            if cfg.target(a.target).is_production:
                with file_lock(cfg.lock_dir, "db", wait=3600, holder="sauvegarde complète après restauration"):
                    tracked(ctx, "db", "full", lambda: (PgBackRest(cfg).backup("full"), {})[1])
        elif a.what == "files":
            if a.push:
                rep = r.push_files(target_name=a.target, when=when, paths=a.path,
                                   force=a.force, confirm=a.confirm)
            else:
                outdir = a.out or os.path.join(cfg.staging_dir, "extraction-" + time.strftime("%Y%m%d-%H%M%S"))
                rep = r.extract_files(when=when, paths=a.path, out_dir=outdir)
        elif a.what == "full":
            # Reprise complète : instance + configuration + modules + filestore.
            confirm_destructive(action="reprise complète", expected=a.target, force=a.force,
                                confirm=a.confirm, description=(
                                    f"Reprise complète sur {a.target} : instance PostgreSQL, "
                                    f"/etc/odoo, modules et filestore seront remplacés."))
            rep = {"cluster": r.restore_cluster(target_name=a.target, when=when, force=True,
                                                confirm=a.target)}
            t = cfg.target(a.target)
            paths = [p for p in cfg.file_sources if not p.startswith("/var/lib/odooguard-export")]
            rep["files"] = r.push_files(target_name=a.target, when=when, paths=paths,
                                        force=True, confirm=a.target)
            from odooguard.remote import Agent
            Agent(t).call("odoo-start", timeout=300)
            rep["odoo"] = "démarré"
        else:  # pragma: no cover
            raise OdooGuardError("type de restauration inconnu", "usage")
    out(ctx, rep, json.dumps(rep, indent=2, ensure_ascii=False, default=str))
    return 0


# ====================================================================== maintenance
def cmd_maintenance(ctx: Ctx) -> int:
    cfg = ctx.cfg

    def job():
        with file_lock(cfg.lock_dir, "files", wait=3600, holder="maintenance (rétention restic)"):
            Restic(cfg).forget(cfg.restic_keep, prune=True)
            for d in cfg.destinations.values():
                if d.enabled:
                    try:
                        dest_mod.preflight(d)
                        dest_mod.restic_for(cfg, d).forget(cfg.restic_keep, prune=True)
                    except OdooGuardError as exc:
                        log.warning("maintenance %s : %s", d.name, exc)
        return {"message": "restic forget --prune effectué"}
    tracked(ctx, "maintenance", "weekly", job)
    return 0


# Lignes de journal sans intérêt pour un humain (sudo, PAM, audit JSON, cycle de vie systemd)
_NOISE = re.compile(r"pam_unix\(|COMMAND=|^\{\"ts\"|^(Started|Starting|Stopping|Stopped|Failed to start) |"
                    r"^\[(CRITIQUE|AVERTISSEMENT|OK)\] |Triggering OnFailure|Consumed .* CPU time|Main process exited|Failed with result|"
                    r"Deactivated successfully|Scheduled restart job")
_STAMP = re.compile(r"^\d{4}-\d\d-\d\d (\d\d:\d\d:\d\d),\d+ (INFO|WARNING|ERROR|DEBUG) ")


def summarize_unit_journal(text: str, max_lines: int = 8) -> tuple[str, list[str]]:
    """(cause probable, dernières lignes utiles) à partir du journal brut d'une unité."""
    useful = []
    for line in text.splitlines():
        line = line.strip()
        if not line or _NOISE.search(line):
            continue
        line = _STAMP.sub(lambda m: f"{m.group(1)} {'' if m.group(2) == 'INFO' else m.group(2) + ' '}", line)
        useful.append(line[:300])
    seen, dedup = set(), []
    for l in useful:
        key = re.sub(r"^\d\d:\d\d:\d\d (ERROR |WARNING )?", "", l)
        if key not in seen:
            seen.add(key)
            dedup.append(l)
    useful = dedup
    errors = [l for l in useful if re.search(r"ERREUR|ERROR|échec|ÉCHEC|Traceback|Error|refus|impossible", l)]
    cause = errors[-1] if errors else (useful[-1] if useful else "aucune ligne explicite dans le journal")
    return cause, useful[-max_lines:]


def cmd_notify_failure(ctx: Ctx) -> int:
    """Appelé par OnFailure= des unités systemd : alerte courte avec la cause probable."""
    unit = ctx.args.unit
    # Seulement l'exécution qui vient d'échouer (pas les précédentes)
    inv = run(["systemctl", "show", unit, "-p", "InvocationID", "--value"], check=False).stdout.strip()
    sel = [f"_SYSTEMD_INVOCATION_ID={inv}"] if re.fullmatch(r"[0-9a-f]{32}", inv) else ["-u", unit]
    res = run(["journalctl", *sel, "-n", "80", "--no-pager", "-o", "cat"], check=False)
    if not res.stdout.strip():
        res = run(["journalctl", "-u", unit, "-n", "60", "--no-pager", "-o", "cat"], check=False)
    desc = run(["systemctl", "show", unit, "-p", "Description", "--value"], check=False).stdout.strip()
    cause, lines = summarize_unit_journal(res.stdout)
    name = unit.removesuffix(".service")
    body = (f"{desc or name} s'est arrêté en erreur.\n\n"
            f"Cause probable :\n  {cause}\n\n"
            "Dernières lignes utiles :\n" + "\n".join(f"  {l}" for l in lines) +
            f"\n\nJournal complet sur OdooGuard : journalctl -u {unit} -n 200")
    notify.send("critical", f"échec du service {name}", body, category="unite")
    return 0


def cmd_alert_test(ctx: Ctx) -> int:
    """Envoie un message de test sur tous les canaux configurés."""
    res = notify.dispatch(ctx.cfg, ctx.args.level, "message de test",
                          "Si vous lisez ceci, ce canal d'alerte OdooGuard fonctionne.",
                          key="test", history=ctx.history, force_all=True)
    for ch, r in res.items():
        print(f"{ch:<10} {r}")
    return 0 if all(r == "ok" for r in res.values()) else 2


def cmd_web_user(ctx: Ctx) -> int:
    import getpass

    from odooguard.web import auth
    path = ctx.cfg.web.users_file
    accounts = auth.read_accounts(path)
    a = ctx.args
    if a.action == "list":
        for name, acc in sorted(accounts.items()):
            print(f"{name:<20} {acc['role']}")
        return 0
    if not auth.valid_username(a.name or ""):
        raise OdooGuardError("nom invalide (2 à 32 caractères : lettres, chiffres, . _ -)", "usage")
    if a.action == "remove":
        if accounts.pop(a.name, None) is None:
            raise OdooGuardError(f"compte inconnu : {a.name}", "usage")
        auth.write_accounts(path, accounts)
        ctx.auditor.record("web-user-remove", a.name, "success")
        print(f"compte {a.name} supprimé")
        return 0
    if a.action == "role":
        if a.name not in accounts:
            raise OdooGuardError(f"compte inconnu : {a.name}", "usage")
        accounts[a.name]["role"] = a.role
        auth.write_accounts(path, accounts)
        ctx.auditor.record("web-user-role", a.name, "success", role=a.role)
        print(f"compte {a.name} : rôle {a.role}")
        return 0
    pw = os.environ.get("ODOOGUARD_NEW_PASSWORD") or getpass.getpass("Mot de passe (12 caractères min.) : ")
    if len(pw) < 12:
        raise OdooGuardError("mot de passe trop court (12 caractères minimum)", "usage")
    if not os.environ.get("ODOOGUARD_NEW_PASSWORD") and getpass.getpass("Confirmation : ") != pw:
        raise OdooGuardError("les deux saisies diffèrent", "usage")
    role = a.role or accounts.get(a.name, {}).get("role", "lecteur")
    accounts[a.name] = {"role": role, "hash": auth.hash_password(pw)}
    auth.write_accounts(path, accounts)
    ctx.auditor.record("web-user-set", a.name, "success", role=role)
    print(f"compte {a.name} enregistré (rôle {role})")
    return 0


def cmd_restore_test(ctx: Ctx) -> int:
    """Test de restauration complet sur la VM de test, avec rapport."""
    from odooguard.restoretest import RestoreTester, format_report
    cfg = ctx.cfg
    holder = {}

    def job():
        report = RestoreTester(cfg, ctx.auditor, ctx.history).run(
            databases=ctx.args.db or None, point=ctx.args.time, send=not ctx.args.no_send)
        holder["report"] = report
        if report["level"] == "critical":
            raise OdooGuardError("test de restauration en échec (rapport envoyé)", "restore-test")
        return {"message": f"test {report['level']}", "label": os.path.basename(report["file"])}
    try:
        with file_lock(cfg.lock_dir, "restore", wait=ctx.args.wait):
            tracked(ctx, "restore-test", "hebdo" if ctx.args.scheduled else "manuel", job, alert=False)
    finally:
        rep = holder.get("report")
        if rep:
            out(ctx, rep, format_report(rep))
    return 0


def cmd_jobs_worker(ctx: Ctx) -> int:
    from odooguard.jobs import worker
    worker(ctx.cfg, ctx.history, once=ctx.args.once)
    return 0


def cmd_web(ctx: Ctx) -> int:
    from odooguard.web.app import serve
    serve(ctx.cfg)
    return 0


def cmd_report(ctx: Ctx) -> int:
    """Enregistre un rapport extérieur à la main (essai, ou script local)."""
    from odooguard import external
    a = ctx.args
    if a.name not in ctx.cfg.externals:
        raise OdooGuardError(f"composant extérieur inconnu : {a.name} (section [external:{a.name}])", "usage")
    r = external.store(ctx.history, a.name, {"status": a.status, "message": a.message,
                                             "finished_at": a.finished_at}, "local")
    print(f"rapport {a.name} enregistré : {r['status']}")
    return 0


def cmd_init(ctx: Ctx) -> int:
    """Initialisation idempotente des dépôts (après l'installation)."""
    cfg = ctx.cfg
    for d in (cfg.repo_path, cfg.wal_spool, cfg.mirror_dir, cfg.staging_dir, cfg.pg_shadow):
        os.makedirs(d, exist_ok=True)
    WalIngest(cfg).refresh_shadow()
    print("pg_control récupéré depuis server01")
    if not os.path.exists(os.path.join(cfg.repo_path, "backup", cfg.stanza, "backup.info")):
        PgBackRest(cfg).stanza_create()
        print(f"stanza pgBackRest « {cfg.stanza} » créée")
    if not os.path.exists(os.path.join(cfg.restic_repo, "config")):
        Restic(cfg).init()
        print(f"dépôt restic initialisé : {cfg.restic_repo}")
    print("Initialisation terminée.")
    return 0


# ====================================================================== parseur
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="odooguard", description="Sauvegarde / restauration Odoo du LNHC")
    p.add_argument("--config", help="fichier de configuration (défaut /etc/odooguard/odooguard.conf)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"odooguard {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backup", help="lancer une sauvegarde")
    bs = b.add_subparsers(dest="what", required=True)
    bdb = bs.add_parser("db", help="base de données (pgBackRest)")
    bdb.add_argument("--type", choices=["full", "diff", "incr"], default="incr")
    bfi = bs.add_parser("files", help="filestore, modules, configuration (rsync + restic)")
    bco = bs.add_parser("copy", help="copie des dépôts vers les NAS")
    bco.add_argument("--dest", help="une seule destination")
    for x in (bdb, bfi, bco):
        x.add_argument("--wait", type=float, default=0,
                       help="attendre le verrou N secondes si une sauvegarde est en cours")
    b.set_defaults(func=cmd_backup)

    w = sub.add_parser("wal-ingest", help="verser les WAL reçus dans le dépôt (service)")
    w.add_argument("--once", action="store_true", help="un seul passage puis sortie")
    w.add_argument("--refresh-shadow", action="store_true",
                   help="re-télécharger pg_control depuis server01")
    w.add_argument("--finalize-partial", action="store_true",
                   help="SINISTRE : verser le segment en cours (server01 perdu)")
    w.add_argument("--force", action="store_true")
    w.add_argument("--confirm")
    w.set_defaults(func=cmd_wal_ingest)

    l = sub.add_parser("list", help="lister les sauvegardes")
    l.add_argument("what", nargs="?", choices=["db", "files", "runs", "all"], default="all")
    l.add_argument("--limit", type=int, default=30)
    l.add_argument("--json", action="store_true")
    l.set_defaults(func=cmd_list)

    v = sub.add_parser("verify", help="vérifier l'intégrité des sauvegardes")
    v.add_argument("--level", choices=["quick", "full"], default="quick",
                   help="quick : contrôles rapides ; full : relecture complète (long)")
    v.add_argument("--dest", help="limiter à une destination")
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=cmd_verify)

    s = sub.add_parser("status", help="état des sauvegardes")
    s.add_argument("--json", action="store_true")
    s.add_argument("--check", action="store_true",
                   help="code retour 0/1/2 (ok/avertissement/critique) et alerte si besoin")
    s.add_argument("--record", action="store_true", help="écrire status.json et les mesures RPO")
    s.add_argument("--no-remote", action="store_true", help="ne pas interroger server01")
    s.add_argument("--rpo-report", metavar="7d", help="perte de données max. mesurée sur N jours")
    s.set_defaults(func=cmd_status)

    r = sub.add_parser("restore", help="restaurer")
    rs = r.add_subparsers(dest="what", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--time", help="instant visé : '2026-11-22 14:30', 'hier 14:30', '-2h'. "
                                       "Absent = le plus récent possible")
    common.add_argument("--force", action="store_true", help="autoriser une opération destructive")
    common.add_argument("--confirm", help="confirmation non interactive (nom exact attendu)")
    common.add_argument("--json", action="store_true")
    rdb = rs.add_parser("db", parents=[common], help="une seule base (+ son filestore)")
    rdb.add_argument("--db", required=True, help="base source, ex. LNHC_Prod_2025")
    rdb.add_argument("--as", dest="as_name", help="nom de la base restaurée (défaut : identique)")
    rdb.add_argument("--target", default="odoo", help="cible : odoo (server01) ou [target:<nom>]")
    rdb.add_argument("--neutralize", choices=["auto", "yes", "no"], default="auto",
                     help="auto : neutralisée sauf si restaurée sous son nom en production")
    rdb.add_argument("--no-filestore", action="store_true")
    rdb.add_argument("--keep-staging", action="store_true", help="garder la zone de travail (diagnostic)")
    rcl = rs.add_parser("cluster", parents=[common], help="toute l'instance PostgreSQL de la cible")
    rcl.add_argument("--target", required=True)
    rfi = rs.add_parser("files", parents=[common], help="fichiers depuis restic")
    rfi.add_argument("--path", action="append", default=[], help="chemin d'origine sur server01")
    rfi.add_argument("--out", help="répertoire d'extraction local")
    rfi.add_argument("--push", action="store_true", help="réinstaller sur la cible au lieu d'extraire")
    rfi.add_argument("--target", default="odoo")
    rfu = rs.add_parser("full", parents=[common], help="reprise complète (serveur Debian neuf)")
    rfu.add_argument("--target", required=True)
    r.set_defaults(func=cmd_restore)

    m = sub.add_parser("maintenance", help="rétention restic (forget --prune)")
    m.set_defaults(func=cmd_maintenance)
    nf = sub.add_parser("notify-failure", help="(systemd OnFailure) alerter sur l'échec d'une unité")
    nf.add_argument("unit")
    nf.set_defaults(func=cmd_notify_failure)
    rt = sub.add_parser("restore-test", help="test de restauration complet sur la VM de test")
    rt.add_argument("--db", action="append", help="base à tester (défaut : [restore_test] databases)")
    rt.add_argument("--time", help="instant restauré (défaut : [restore_test] point, « hier 16:00 »)")
    rt.add_argument("--no-send", action="store_true", help="ne pas envoyer le rapport")
    rt.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    rt.add_argument("--wait", type=float, default=0)
    rt.add_argument("--json", action="store_true")
    rt.set_defaults(func=cmd_restore_test)
    jw = sub.add_parser("jobs-worker", help="(service) exécuter les travaux demandés depuis le web")
    jw.add_argument("--once", action="store_true")
    jw.set_defaults(func=cmd_jobs_worker)
    at = sub.add_parser("alert-test", help="envoyer un message de test sur tous les canaux")
    at.add_argument("--level", choices=["warning", "critical"], default="warning")
    at.set_defaults(func=cmd_alert_test)
    wu = sub.add_parser("web-user", help="comptes du tableau de bord")
    wu.add_argument("action", choices=["add", "role", "remove", "list"])
    wu.add_argument("name", nargs="?")
    wu.add_argument("role", nargs="?", choices=["lecteur", "operateur", "admin"],
                    help="rôle (add, role) : lecteur, operateur, admin")
    wu.set_defaults(func=cmd_web_user)
    wb = sub.add_parser("web", help="lancer le tableau de bord (service odooguard-web)")
    wb.set_defaults(func=cmd_web)
    rp = sub.add_parser("report", help="enregistrer un rapport extérieur (Veeam, copie G:)")
    rp.add_argument("name")
    rp.add_argument("--status", choices=["success", "warning", "failed"], required=True)
    rp.add_argument("--message", default="")
    rp.add_argument("--finished-at", dest="finished_at")
    rp.set_defaults(func=cmd_report)
    i = sub.add_parser("init", help="initialiser les dépôts (une fois, après installation)")
    i.set_defaults(func=cmd_init)
    return p


def _drop_privileges_if_root(run_as: str) -> None:
    """Lancé en root (sudo) : on se ré-exécute sous le compte de service."""
    if os.geteuid() != 0 or os.environ.get("ODOOGUARD_ALLOW_ROOT") == "1":
        return
    try:
        pwd.getpwnam(run_as)
    except KeyError:
        return
    os.execvp("runuser", ["runuser", "-u", run_as, "--", sys.executable, "-m", "odooguard",
                          *sys.argv[1:]])


_STOPPED_BY_SYSTEMD = [False]


def _sigterm(_signum, _frame):
    _STOPPED_BY_SYSTEMD[0] = True
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    if argv is None:
        _drop_privileges_if_root("odooguard")
        # systemctl stop envoie SIGTERM : traité comme Ctrl+C pour que l'historique
        # enregistre « interrompue » au lieu de rester « en cours ».
        signal.signal(signal.SIGTERM, _sigterm)
    try:
        ctx = Ctx(args)
        return int(args.func(ctx) or 0)
    except config_mod.ConfigError as exc:
        print(f"configuration : {exc}", file=sys.stderr)
        return 3
    except OdooGuardError as exc:
        print(f"ERREUR [{exc.category}] : {exc}", file=sys.stderr)
        return 3 if exc.category == "usage" else 2
    except KeyboardInterrupt:
        if _STOPPED_BY_SYSTEMD[0]:
            # Arrêt demandé (systemctl stop/restart, mise à jour, extinction) : ce n'est pas
            # une panne. L'exécution est notée « interrompue » dans l'historique ; code 0 pour
            # que systemd ne déclenche pas l'alerte « l'unité a échoué ».
            print("arrêt demandé : interrompu proprement", file=sys.stderr)
            return 0
        print("interrompu", file=sys.stderr)
        return 130
