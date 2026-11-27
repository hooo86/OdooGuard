"""Restauration : instance complète, une seule base, filestore, reprise totale.

Principes de sécurité :
* rien n'est jamais supprimé sur la cible : une base ou un répertoire écrasé
  est d'abord RENOMMÉ (suffixe ``_avant_restauration_<horodatage>``) ;
* toute opération qui remplace des données exige --force + confirmation
  explicite, et est journalisée (audit) avant et après ;
* une copie restaurée sous un autre nom ou sur une autre machine que la
  production est neutralisée (``odoo neutralize``) par défaut : pas d'envoi
  de courriels, crons désactivés, etc.

Restauration d'une seule base (cas le plus courant) :
  1. pgBackRest restaure l'instance sur OdooGuard, dans un répertoire de
     travail, en ne gardant que la base demandée (--db-include), jusqu'à
     l'instant demandé (rejeu des WAL) ;
  2. une instance PostgreSQL temporaire (socket Unix privée, aucun port
     réseau) est démarrée sur ces fichiers ;
  3. ``pg_dump`` de cette base est transmis en flux à l'agent de la cible,
     qui crée la base et la recharge avec ``pg_restore`` ;
  4. le filestore de l'instantané restic le plus proche (antérieur) suit ;
  5. neutralisation, rapport de contrôle, nettoyage du répertoire de travail.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid

from odooguard.audit import Auditor, confirm_destructive
from odooguard.config import Config
from odooguard.files import Restic, mirror_path
from odooguard.pg import PgBackRest
from odooguard.remote import Agent
from odooguard.util import (CommandError, OdooGuardError, check_free_space, fmt_time,
                            pg_time, run)

log = logging.getLogger("odooguard")

DB_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}$")


def validate_db_name(name: str) -> str:
    if not DB_NAME_RE.match(name):
        raise OdooGuardError(f"nom de base invalide : « {name} »", "usage")
    return name


def recovery_args(when: dt.datetime | None) -> list[str]:
    """Options pgBackRest de cible de restauration."""
    if when is None:
        # Jusqu'au dernier WAL disponible (le plus récent possible).
        return ["--type=default"]
    return ["--type=time", f"--target={pg_time(when)}", "--target-action=promote"]


class StagingCluster:
    """Instance PostgreSQL temporaire sur OdooGuard, isolée du réseau."""

    def __init__(self, cfg: Config, root: str):
        self.cfg = cfg
        self.root = root
        self.pgdata = os.path.join(root, "pgdata")
        self.sock = os.path.join(root, "sock")
        self.port = "5499"
        self.bin = cfg.pg_bin_dir

    def restore(self, db: str | None, when: dt.datetime | None) -> None:
        os.makedirs(self.pgdata, mode=0o700, exist_ok=True)
        os.makedirs(self.sock, mode=0o700, exist_ok=True)
        args = [f"--pg1-path={self.pgdata}", *recovery_args(when),
                f"--recovery-option=restore_command={self.restore_command()}"]
        if db:
            args.append(f"--db-include={db}")
        # La configuration « ingest » désigne le dépôt LOCAL : la restauration
        # sur OdooGuard ne dépend ni de server01 ni du réseau.
        PgBackRest(self.cfg).exec(*args, "restore", config=self.cfg.ingest_config)
        # Paquet Debian : postgresql.conf est dans /etc, pas dans PGDATA. On écrit
        # une configuration minimale. hot_standby=off : aucune connexion avant la
        # fin du rejeu, et pas d'exigence sur max_connections & co.
        with open(os.path.join(self.pgdata, "postgresql.conf"), "w") as fh:
            fh.write(
                "# Instance temporaire OdooGuard - générée automatiquement\n"
                "listen_addresses = ''\n"
                f"port = {self.port}\n"
                f"unix_socket_directories = '{self.sock}'\n"
                "hot_standby = off\n"
                "archive_mode = off\n"
                "max_wal_senders = 0\n"
                "shared_buffers = 512MB\n"
                "maintenance_work_mem = 512MB\n"
                "max_connections = 30\n"
                "fsync = off\n"  # instance jetable : vitesse avant durabilité
                "full_page_writes = off\n"
                f"hba_file = '{self.pgdata}/pg_hba.conf'\n"
                f"ident_file = '{self.pgdata}/pg_ident.conf'\n"
            )
        with open(os.path.join(self.pgdata, "pg_hba.conf"), "w") as fh:
            fh.write("local all all trust\n")
        open(os.path.join(self.pgdata, "pg_ident.conf"), "a").close()

    def restore_command(self) -> str:
        """WAL depuis le dépôt, à défaut le segment en cours de réception.

        Le dernier segment (``.partial`` de pg_receivewal) n'est pas encore
        dans le dépôt : sans ce repli, « restaurer à il y a 2 minutes »
        s'arrêterait au dernier segment complet.
        """
        return (f"{self.cfg.pgbackrest_bin} --config={self.cfg.ingest_config} "
                f"--stanza={self.cfg.stanza} --pg1-path={self.pgdata} archive-get %f \"%p\" || "
                f"cp {self.cfg.wal_spool}/%f.partial \"%p\"")

    def start(self, timeout_s: int = 6 * 3600) -> None:
        run([os.path.join(self.bin, "pg_ctl"), "-D", self.pgdata, "-l",
             os.path.join(self.root, "postgresql.log"), "-w", "-t", "60", "start"],
            timeout=180)
        # hot_standby=off : pg_ctl rend la main pendant le rejeu des WAL ; on
        # attend que l'instance accepte les connexions ET soit promue.
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            res = run([os.path.join(self.bin, "psql"), "-h", self.sock, "-p", self.port,
                       "-U", "postgres", "-d", "postgres", "-XAtqc", "select pg_is_in_recovery()"],
                      check=False, timeout=60)
            if res.returncode == 0 and res.stdout.strip() == "f":
                return
            if not os.path.exists(os.path.join(self.pgdata, "postmaster.pid")):
                raise OdooGuardError("l'instance temporaire s'est arrêtée pendant le rejeu des WAL :\n"
                                     + self.log_tail(), "command")
            time.sleep(3)
        raise OdooGuardError("l'instance temporaire ne sort pas de la restauration", "command")

    def log_tail(self, n: int = 12) -> str:
        try:
            with open(os.path.join(self.root, "postgresql.log"), encoding="utf-8",
                      errors="replace") as fh:
                return "\n".join(fh.read().splitlines()[-n:])
        except OSError:
            return ""

    def psql(self, db: str, sql: str) -> str:
        return run([os.path.join(self.bin, "psql"), "-h", self.sock, "-p", self.port,
                    "-U", "postgres", "-d", db, "-XAtqc", sql], timeout=600).stdout

    def dump_cmd(self, db: str) -> list[str]:
        return [os.path.join(self.bin, "pg_dump"), "-h", self.sock, "-p", self.port,
                "-U", "postgres", "-Fc", "-Z", "1", "--no-owner", "--no-acl", "-d", db]

    def stop(self) -> None:
        if os.path.exists(os.path.join(self.pgdata, "postmaster.pid")):
            run([os.path.join(self.bin, "pg_ctl"), "-D", self.pgdata, "-m", "fast", "stop"],
                check=False, timeout=600)

    def recovery_point(self) -> str:
        """Dernière transaction rejouée : preuve de l'instant réellement atteint."""
        try:
            log_path = os.path.join(self.root, "postgresql.log")
            lines = open(log_path, encoding="utf-8", errors="replace").read().splitlines()
            hits = [l for l in lines if "recovery stopping" in l or "last completed transaction" in l]
            return hits[-1] if hits else ""
        except OSError:
            return ""


class Restorer:
    def __init__(self, cfg: Config, auditor: Auditor):
        self.cfg = cfg
        self.auditor = auditor

    # ------------------------------------------------------------ une base
    def restore_db(self, *, db: str, as_name: str | None, when: dt.datetime | None,
                   target_name: str, neutralize: bool | None, with_filestore: bool,
                   force: bool, confirm: str | None, keep_staging: bool = False) -> dict:
        validate_db_name(db)
        as_name = validate_db_name(as_name or db)
        target = self.cfg.target(target_name)
        agent = Agent(target)
        if neutralize is None:
            neutralize = not (target.is_production and as_name == db)

        exists = agent.call_json("db-exists", as_name).get("exists", False)
        replace = bool(exists)
        if exists:
            confirm_destructive(
                action="remplacement de base", expected=as_name, force=force, confirm=confirm,
                description=(f"La base « {as_name} » existe sur {target.name} ({target.host}). "
                             f"Elle sera RENOMMÉE en « {as_name}_avant_restauration_<date> » "
                             f"(pas supprimée), puis remplacée par « {db} » "
                             f"au {fmt_time(when) if when else 'dernier état connu'}."))

        details = {"db": db, "as": as_name, "when": fmt_time(when) if when else "latest",
                   "target": target.name, "neutralize": neutralize, "replace": replace}
        self.auditor.record("restore-db", f"{target.name}:{as_name}", "started", **details)
        check_free_space(self.cfg.staging_dir, self.cfg.min_free_gb, "zone de travail")
        root = os.path.join(self.cfg.staging_dir, f"restore-{time.strftime('%Y%m%d-%H%M%S')}-"
                            f"{uuid.uuid4().hex[:6]}")
        staging = StagingCluster(self.cfg, root)
        report: dict = {**details}
        try:
            t0 = time.monotonic()
            staging.restore(db, when)
            staging.start()
            report["recovery_point"] = staging.recovery_point()
            report["pg_restore_s"] = round(time.monotonic() - t0)

            # pg_dump (OdooGuard) -> pg_restore (cible), en flux, sans fichier intermédiaire
            t1 = time.monotonic()
            args = ["db-import", as_name] + (["--replace"] if replace else [])
            dump = subprocess.Popen(staging.dump_cmd(db), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            try:
                agent.stream_in(*args, source=dump.stdout)
            finally:
                dump.stdout.close()
                dump_rc = dump.wait()
            if dump_rc != 0:
                raise OdooGuardError(f"pg_dump a échoué : {dump.stderr.read().decode()[-2000:]}",
                                     "command")
            report["transfer_s"] = round(time.monotonic() - t1)

            if with_filestore:
                report["filestore"] = self._restore_filestore(
                    agent, db, as_name, when, root, replace)
            if neutralize:
                agent.call("neutralize", as_name, timeout=1800)
                report["neutralized"] = True
            report["control"] = agent.call_json("db-report", as_name, timeout=600)
        except Exception as exc:
            self.auditor.record("restore-db", f"{target.name}:{as_name}", "failed",
                                error=str(exc)[:1000], **details)
            raise
        finally:
            staging.stop()
            if not keep_staging:
                shutil.rmtree(root, ignore_errors=True)
        self.auditor.record("restore-db", f"{target.name}:{as_name}", "success", **report)
        return report

    def _restore_filestore(self, agent: Agent, db: str, as_name: str,
                           when: dt.datetime | None, root: str, replace: bool) -> dict:
        restic = Restic(self.cfg)
        when_ref = when or dt.datetime.now(dt.timezone.utc)
        snap = restic.snapshot_at(when_ref)
        src = os.path.join(mirror_path(self.cfg, self.cfg.source.filestore_path), db)
        out = os.path.join(root, "fs")
        restic.restore(snap["id"], out, [src])
        local = os.path.join(out, src.lstrip("/"))
        if not os.path.isdir(local):
            raise OdooGuardError(f"filestore de {db} absent de l'instantané {snap['short_id']}",
                                 "command")
        args = ["filestore-import", as_name] + (["--replace"] if replace else [])
        tar = subprocess.Popen(["tar", "-C", local, "-cf", "-", "."], stdout=subprocess.PIPE)
        try:
            agent.stream_in(*args, source=tar.stdout)
        finally:
            tar.stdout.close()
            if tar.wait() != 0:
                raise OdooGuardError("tar du filestore a échoué", "command")
        return {"snapshot": snap["short_id"], "snapshot_time": snap["time"]}

    # ------------------------------------------------------------ instance
    def restore_cluster(self, *, target_name: str, when: dt.datetime | None, force: bool,
                        confirm: str | None) -> dict:
        """Remplace TOUTE l'instance PostgreSQL de la cible (toutes les bases)."""
        target = self.cfg.target(target_name)
        confirm_destructive(
            action="restauration de l'instance", expected=target.name, force=force,
            confirm=confirm,
            description=(f"TOUTE l'instance PostgreSQL {target.pg_version}/{target.pg_cluster} de "
                         f"{target.name} ({target.host}) va être remplacée "
                         f"(état au {fmt_time(when) if when else 'dernier WAL reçu'}). Odoo sera "
                         f"arrêté. L'ancien répertoire de données est conservé (renommé)."))
        details = {"target": target.name, "when": fmt_time(when) if when else "latest"}
        self.auditor.record("restore-cluster", target.name, "started", **details)
        args = ["pg-restore-cluster", target.pg_version, target.pg_cluster, self.cfg.stanza,
                agent_time(when) if when else "latest"]
        try:
            result = agent_json(Agent(target), *args, timeout=12 * 3600)
        except Exception as exc:
            self.auditor.record("restore-cluster", target.name, "failed", error=str(exc)[:1000])
            raise
        if target.is_production:
            # Nouvelle ligne temporelle : recréer le slot, relancer la réception
            # des WAL, puis une sauvegarde complète (faite par l'appelant).
            Agent(target).call("create-slot", target.pg_version, target.pg_cluster)
            run(["sudo", "-n", "/usr/bin/systemctl", "restart", self.cfg.wal_receiver_unit],
                check=False)
            result["next_step"] = "sauvegarde complète lancée (nouvelle ligne temporelle)"
        self.auditor.record("restore-cluster", target.name, "success", **details, **result)
        return result

    # ------------------------------------------------------------ fichiers
    def extract_files(self, *, when: dt.datetime | None, paths: list[str], out_dir: str) -> dict:
        """Extraction locale (non destructive) de fichiers d'un instantané restic."""
        restic = Restic(self.cfg)
        snap = restic.snapshot_at(when or dt.datetime.now(dt.timezone.utc))
        includes = [mirror_path(self.cfg, p) for p in paths] if paths else []
        os.makedirs(out_dir, exist_ok=True)
        restic.restore(snap["id"], out_dir, includes)
        return {"snapshot": snap["short_id"], "time": snap["time"], "out": out_dir}

    def push_files(self, *, target_name: str, when: dt.datetime | None, paths: list[str],
                   force: bool, confirm: str | None) -> dict:
        """Réinstalle des répertoires (config, modules, filestore) sur la cible."""
        target = self.cfg.target(target_name)
        confirm_destructive(
            action="réinstallation de fichiers", expected=target.name, force=force,
            confirm=confirm,
            description=(f"Les répertoires {', '.join(paths)} de {target.name} vont être "
                         f"remplacés (les existants sont renommés, pas supprimés)."))
        root = os.path.join(self.cfg.staging_dir, f"files-{uuid.uuid4().hex[:8]}")
        try:
            info = self.extract_files(when=when, paths=paths, out_dir=root)
            agent = Agent(target)
            for p in paths:
                local = os.path.join(root, mirror_path(self.cfg, p).lstrip("/"))
                if not os.path.isdir(local):
                    raise OdooGuardError(f"{p} absent de l'instantané {info['snapshot']}", "usage")
                tar = subprocess.Popen(["tar", "-C", local, "-cf", "-", "."],
                                       stdout=subprocess.PIPE)
                try:
                    agent.stream_in("fs-import", p, source=tar.stdout)
                finally:
                    tar.stdout.close()
                    tar.wait()
                self.auditor.record("push-files", f"{target.name}:{p}", "success",
                                    snapshot=info["snapshot"])
        finally:
            shutil.rmtree(root, ignore_errors=True)
        return info


def agent_time(when: dt.datetime) -> str:
    """Horodatage sans espace pour l'agent (validé par expression régulière)."""
    return when.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def agent_json(agent: Agent, *args: str, timeout: float) -> dict:
    try:
        res = agent.call(*args, timeout=timeout)
    except CommandError as exc:
        raise OdooGuardError(f"agent {agent.target.name} : {exc}", exc.category) from exc
    lines = [l for l in res.stdout.splitlines() if l.startswith("{")]
    return json.loads(lines[-1]) if lines else {"output": res.stdout[-2000:]}
