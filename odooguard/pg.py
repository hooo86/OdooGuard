"""Sauvegarde PostgreSQL : pgBackRest + collecte des WAL en mode « pull ».

Architecture (voir docs/architecture.md) :

* Les sauvegardes complètes / différentielles / incrémentales sont lancées
  sur OdooGuard (hôte du dépôt) ; pgBackRest se connecte en SSH à server01
  (compte ``postgres``, commande forcée ``odooguard-pgbr-gate``) pour lire
  les fichiers de données. server01 n'a aucun accès au dépôt.

* Les WAL ne sont PAS poussés par ``archive_command`` (cela donnerait à
  server01 un accès en écriture au dépôt). Ils sont tirés en continu par
  ``pg_receivewal`` (service ``odooguard-walreceiver``, slot de réplication
  ``odooguard``) dans un répertoire tampon local, puis versés dans le dépôt
  par ``odooguard wal-ingest`` via ``pgbackrest archive-push`` exécuté
  localement. ``archive-push`` vérifie l'identifiant système de l'instance
  dans une copie de ``pg_control`` (« pg_shadow ») récupérée sur server01.

  Effet de bord positif : la perte de données maximale n'est plus bornée par
  la fréquence des sauvegardes mais par le flux de réplication (quelques
  secondes), le segment en cours (``.partial``) étant lui aussi sur OdooGuard.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass

from odooguard.config import Config
from odooguard.remote import Agent
from odooguard.util import CommandError, OdooGuardError, Result, run

log = logging.getLogger("odooguard")

WAL_RE = re.compile(r"^[0-9A-F]{24}$")
HISTORY_RE = re.compile(r"^[0-9A-F]{8}\.history$")
PARTIAL_RE = re.compile(r"^[0-9A-F]{24}\.partial$")
PUSHED_DIR = ".pushed"


@dataclass
class BackupInfo:
    label: str
    type: str
    start: int
    stop: int
    size: int
    repo_size: int
    repo_delta: int
    wal_start: str
    wal_stop: str
    error: bool

    @property
    def duration(self) -> int:
        return self.stop - self.start


@dataclass
class StanzaInfo:
    status_code: int
    status_message: str
    backups: list[BackupInfo]
    wal_min: str | None
    wal_max: str | None
    pg_version: str | None
    system_id: int | None

    def last(self, types: tuple[str, ...] = ("full", "diff", "incr")) -> BackupInfo | None:
        valid = [b for b in self.backups if b.type in types and not b.error]
        return valid[-1] if valid else None


def parse_info(raw: str, stanza: str) -> StanzaInfo:
    data = json.loads(raw)
    entry = next((s for s in data if s.get("name") == stanza), None)
    if entry is None:
        raise OdooGuardError(f"stanza « {stanza} » absente du dépôt pgBackRest", "config")
    backups = []
    for b in entry.get("backup", []):
        backups.append(BackupInfo(
            label=b["label"], type=b["type"],
            start=b["timestamp"]["start"], stop=b["timestamp"]["stop"],
            size=b["info"]["size"], repo_size=b["info"]["repository"]["size"],
            repo_delta=b["info"]["repository"]["delta"],
            wal_start=(b.get("archive") or {}).get("start") or "",
            wal_stop=(b.get("archive") or {}).get("stop") or "",
            error=bool(b.get("error", False)),
        ))
    archives = entry.get("archive") or []
    wal_min = min((a["min"] for a in archives if a.get("min")), default=None)
    wal_max = max((a["max"] for a in archives if a.get("max")), default=None)
    db = (entry.get("db") or [{}])[-1]
    return StanzaInfo(
        status_code=entry["status"]["code"],
        status_message=entry["status"]["message"],
        backups=backups, wal_min=wal_min, wal_max=wal_max,
        pg_version=db.get("version"), system_id=db.get("system-id"),
    )


class PgBackRest:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _cmd(self, *args: str, config: str | None = None) -> list[str]:
        return [self.cfg.pgbackrest_bin, f"--config={config or self.cfg.pgbackrest_config}",
                f"--stanza={self.cfg.stanza}", *args]

    def exec(self, *args: str, config: str | None = None, timeout: float | None = None,
             check: bool = True) -> Result:
        return run(self._cmd(*args, config=config), timeout=timeout, check=check)

    # -------------------------------------------------------------- lecture
    def info(self, repo_path: str | None = None) -> StanzaInfo:
        args = ["--output=json"]
        if repo_path:
            args.append(f"--repo1-path={repo_path}")
        res = self.exec(*args, "info", timeout=300)
        return parse_info(res.stdout, self.cfg.stanza)

    def database_list(self, label: str | None = None) -> list[str]:
        """Bases contenues dans une sauvegarde (la dernière par défaut), hors bases système."""
        if not label:
            last = self.info().last()
            if not last:
                return []
            label = last.label
        res = self.exec("--output=json", f"--set={label}", "info", timeout=300)
        names = []
        for entry in json.loads(res.stdout):
            for b in entry.get("backup", []):
                for ref in b.get("database-ref") or []:
                    name = ref.get("name") if isinstance(ref, dict) else None
                    if name and name not in ("postgres", "template0", "template1") and name not in names:
                        names.append(name)
        return sorted(names)

    def backup_progress(self) -> dict | None:
        """Progression de la sauvegarde en cours (verrou « backup » de pgBackRest).

        Renvoie {"held": True, "size": total, "size-cplt": copié} ou None. Le compteur
        est mis à jour par pgBackRest à chaque fichier de données terminé.
        """
        res = self.exec("--output=json", "info", timeout=120)
        data = json.loads(res.stdout)
        entry = next((s for s in data if s.get("name") == self.cfg.stanza), None)
        lock = (((entry or {}).get("status") or {}).get("lock") or {}).get("backup") or {}
        return lock if lock.get("held") else None

    # -------------------------------------------------------------- actions
    def backup(self, kind: str) -> Result:
        if kind not in ("full", "diff", "incr"):
            raise OdooGuardError(f"type de sauvegarde inconnu : {kind}", "usage")
        # « expire » est exécuté automatiquement par pgBackRest après une
        # sauvegarde réussie, selon repo1-retention-full / -diff : ceux réglés depuis le
        # tableau de bord ([pgbackrest] retention_*) priment sur pgbackrest.conf.
        return self.exec(*self.retention_args(), f"--type={kind}", "backup")

    def retention_args(self) -> list[str]:
        args = []
        if self.cfg.retention_full:
            args += ["--repo1-retention-full-type=count", f"--repo1-retention-full={self.cfg.retention_full}"]
        if self.cfg.retention_diff:
            args.append(f"--repo1-retention-diff={self.cfg.retention_diff}")
        return args

    def retention_effective(self) -> dict:
        """Rétention réellement appliquée : tableau de bord, sinon pgbackrest.conf."""
        conf = {}
        try:
            for line in open(self.cfg.pgbackrest_config, encoding="utf-8"):
                k, _, v = line.partition("=")
                conf[k.strip()] = v.strip()
        except OSError:
            pass
        def num(v):
            return int(v) if str(v).isdigit() else None
        return {"full": self.cfg.retention_full or num(conf.get("repo1-retention-full")),
                "diff": self.cfg.retention_diff or num(conf.get("repo1-retention-diff"))}

    def check(self) -> Result:
        return self.exec("check", timeout=900)

    def verify(self, repo_path: str | None = None) -> Result:
        args = [f"--repo1-path={repo_path}"] if repo_path else []
        return self.exec(*args, "verify")

    def stanza_create(self) -> Result:
        return self.exec("stanza-create", timeout=600)


# ====================================================================== WAL
class WalIngest:
    """Verse dans le dépôt les segments WAL reçus par pg_receivewal."""

    def __init__(self, cfg: Config, pgbr: PgBackRest | None = None):
        self.cfg = cfg
        self.pgbr = pgbr or PgBackRest(cfg)
        self.spool = cfg.wal_spool
        self.pushed_dir = os.path.join(self.spool, PUSHED_DIR)

    def pending(self) -> list[str]:
        """Fichiers complets non encore versés, dans l'ordre (historiques d'abord)."""
        try:
            names = os.listdir(self.spool)
        except FileNotFoundError:
            raise OdooGuardError(f"répertoire tampon WAL absent : {self.spool}", "config") from None
        done = set(os.listdir(self.pushed_dir)) if os.path.isdir(self.pushed_dir) else set()
        hist = sorted(n for n in names if HISTORY_RE.match(n) and n not in done)
        wal = sorted(n for n in names if WAL_RE.match(n) and n not in done)
        return hist + wal

    def push(self, name: str) -> None:
        path = os.path.join(self.spool, name)
        try:
            self.pgbr.exec("archive-push", path, config=self.cfg.ingest_config, timeout=600)
        except CommandError as exc:
            if "system-id" in exc.stderr or "does not match stanza" in exc.stderr:
                raise OdooGuardError(
                    "l'identifiant système de PostgreSQL ne correspond plus au dépôt "
                    "(serveur réinstallé / restauré ?) : rafraîchir pg_shadow "
                    "(odooguard wal-ingest --refresh-shadow) et lancer stanza-upgrade",
                    "config") from exc
            raise
        os.makedirs(self.pushed_dir, exist_ok=True)
        # Marqueur « versé » : fichier vide, nommé comme le segment.
        open(os.path.join(self.pushed_dir, name), "w").close()

    def cleanup(self) -> int:
        """Supprime du tampon les segments déjà versés, au-delà des N plus récents."""
        if not os.path.isdir(self.pushed_dir):
            return 0
        done = sorted(n for n in os.listdir(self.pushed_dir) if WAL_RE.match(n))
        removed = 0
        for name in done[: max(0, len(done) - self.cfg.wal_keep_local)]:
            for p in (os.path.join(self.spool, name), os.path.join(self.pushed_dir, name)):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass
            removed += 1
        return removed

    def finalize_partial(self) -> str:
        """SINISTRE UNIQUEMENT (server01 perdu) : verse le segment en cours.

        pg_receivewal remplit le segment ``.partial`` au fil de l'eau ; tant que
        server01 vit, il sera complété puis versé normalement. Si server01 est
        perdu, ce segment contient les toutes dernières transactions : on le
        verse sous son nom définitif (le reste du fichier est à zéro, ce que
        PostgreSQL interprète comme la fin des WAL). À ne PAS faire si server01
        fonctionne encore : le vrai segment entrerait ensuite en conflit.
        """
        partial = spool_state(self.cfg)["partial"]
        if not partial:
            raise OdooGuardError("aucun segment .partial dans le tampon", "usage")
        final = partial[: -len(".partial")]
        dst = os.path.join(self.spool, final)
        if os.path.exists(dst):
            raise OdooGuardError(f"{final} existe déjà dans le tampon", "usage")
        shutil.copyfile(os.path.join(self.spool, partial), dst)
        self.push(final)
        return final

    def run_once(self) -> int:
        count = 0
        for name in self.pending():
            self.push(name)
            count += 1
        self.cleanup()
        return count

    def refresh_shadow(self) -> None:
        """Récupère pg_control (lecture seule, via l'agent) pour archive-push local."""
        data = Agent(self.cfg.source).call_bytes(
            "pg-control", self.cfg.source.pg_version, self.cfg.source.pg_cluster)
        if len(data) < 512:
            raise OdooGuardError("pg_control reçu invalide (trop court)", "command")
        gdir = os.path.join(self.cfg.pg_shadow, "global")
        os.makedirs(gdir, exist_ok=True)
        tmp = os.path.join(gdir, "pg_control.tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, os.path.join(gdir, "pg_control"))
        with open(os.path.join(self.cfg.pg_shadow, "PG_VERSION"), "w") as fh:
            fh.write(self.cfg.source.pg_version + "\n")

    def loop(self, interval: float = 2.0, on_error=None) -> None:
        """Boucle du service odooguard-walingest (redémarré par systemd si besoin)."""
        backoff = interval
        while True:
            try:
                n = self.run_once()
                if n:
                    log.info("%d fichier(s) WAL versé(s) dans le dépôt", n)
                backoff = interval
            except OdooGuardError as exc:
                log.error("versement WAL : %s", exc)
                if on_error:
                    on_error(exc)
                backoff = min(backoff * 2, 60)
            time.sleep(backoff)


def spool_state(cfg: Config) -> dict:
    """État du tampon local de WAL (sans interroger server01)."""
    try:
        entries = os.listdir(cfg.wal_spool)
    except FileNotFoundError:
        return {"last_write": None, "pending": None, "partial": None}
    latest = 0.0
    partial = None
    for name in entries:
        if WAL_RE.match(name) or PARTIAL_RE.match(name) or HISTORY_RE.match(name):
            m = os.stat(os.path.join(cfg.wal_spool, name)).st_mtime
            latest = max(latest, m)
            if PARTIAL_RE.match(name):
                partial = name
    try:
        pending = len(WalIngest(cfg).pending())
    except OdooGuardError:
        pending = None
    return {"last_write": latest or None, "pending": pending, "partial": partial}


def wal_state(cfg: Config, *, query_source: bool = True) -> dict:
    """Perte de données maximale actuelle (RPO instantané) pour le flux WAL.

    * Si server01 répond : on lit ``pg_stat_replication`` / ``pg_replication_slots``
      via l'agent (requête locale en lecture seule). Flux actif => le retard est
      ``flush_lag`` (souvent < 1 s) ; flux coupé => retard = temps écoulé depuis
      la dernière réception (« reply_time » ou dernière écriture du tampon).
    * Si server01 ne répond pas : on se rabat sur la date de dernière écriture
      dans le tampon local, estimation prudente (majorante).
    """
    spool = spool_state(cfg)
    state = {**spool, "streaming": None, "lag_seconds": None, "lag_bytes": None,
             "slot_active": None, "slot_wal_status": None, "source": "spool"}
    remote = None
    if query_source:
        try:
            remote = Agent(cfg.source).call_json("wal-status", cfg.source.pg_version,
                                                 cfg.source.pg_cluster, timeout=60)
        except OdooGuardError as exc:
            state["error"] = str(exc).splitlines()[0]
    now_ts = time.time()
    if remote:
        state.update(source="server", streaming=remote.get("streaming"),
                     lag_bytes=remote.get("lag_bytes"), slot_active=remote.get("slot_active"),
                     slot_wal_status=remote.get("slot_wal_status"))
        if remote.get("streaming"):
            state["lag_seconds"] = float(remote.get("flush_lag_s") or 0.0)
        elif spool["last_write"]:
            state["lag_seconds"] = now_ts - spool["last_write"]
    elif spool["last_write"]:
        state["lag_seconds"] = now_ts - spool["last_write"]
    lag = state["lag_seconds"]
    state["ok"] = lag is not None and lag < cfg.max_age["wal"] and (spool["pending"] or 0) < 32
    return state
