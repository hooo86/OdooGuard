"""Sauvegarde des fichiers : filestore, modules additionnels, configuration.

Étape 1 - miroir : ``rsync`` tire (pull) les répertoires de server01 vers un
miroir local d'OdooGuard. Côté server01, la clé d'OdooGuard est limitée par
l'agent à ``rrsync -ro`` (lecture seule) : rsync ne peut ni écrire ni supprimer
sur server01. Seuls les fichiers modifiés transitent (quelques Mo par heure).

Étape 2 - instantané : ``restic`` sauvegarde le miroir dans un dépôt local
dédupliqué et chiffré (AES-256). Un instantané n'existe que si la sauvegarde
est allée au bout ; si rsync a échoué, aucun instantané n'est créé.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re

from odooguard.config import Config
from odooguard.remote import Agent
from odooguard.progress import parse_restic_status, parse_rsync_progress
from odooguard.util import OdooGuardError, Result, in_business_hours, run, run_streaming

log = logging.getLogger("odooguard")

RESTIC_TAG = "odooguard"
RSYNC_OK = (0, 24)  # 24 = fichiers disparus pendant la copie : normal sur un filestore actif


class Restic:
    def __init__(self, cfg: Config, repo: str | None = None, password_file: str | None = None):
        self.cfg = cfg
        self.repo = repo or cfg.restic_repo
        self.password_file = password_file or cfg.restic_password_file

    @property
    def env(self) -> dict[str, str]:
        return {
            "RESTIC_REPOSITORY": self.repo,
            "RESTIC_PASSWORD_FILE": self.password_file,
            "RESTIC_CACHE_DIR": os.path.join(self.cfg.state_dir, "restic-cache"),
            "RESTIC_PROGRESS_FPS": "0.016",
        }

    def exec(self, *args: str, timeout: float | None = None, ok_codes=(0,)) -> Result:
        return run([self.cfg.restic_bin, *args], env=self.env, timeout=timeout, ok_codes=ok_codes)

    def init(self, copy_from: "Restic | None" = None) -> Result:
        args = ["init"]
        if copy_from:
            # Mêmes paramètres de découpage : la copie reste dédupliquée.
            args += ["--from-repo", copy_from.repo, "--from-password-file",
                     copy_from.password_file, "--copy-chunker-params"]
        return self.exec(*args, timeout=600)

    def snapshots(self) -> list[dict]:
        # --no-lock : simple lecture, possible même sur un dépôt monté en lecture
        # seule (service web cloisonné) ou pendant une sauvegarde.
        res = self.exec("--no-lock", "snapshots", "--json", "--tag", RESTIC_TAG, timeout=600)
        snaps = json.loads(res.stdout or "[]")
        snaps.sort(key=lambda s: s["time"])
        return snaps

    def backup(self, path: str, host: str, tags: list[str], progress=None) -> dict:
        args = ["backup", "--json", "--host", host, "--tag", RESTIC_TAG]
        for t in tags:
            args += ["--tag", t]

        def on_line(line: str) -> None:
            if progress is not None:
                st = parse_restic_status(line)
                if st:
                    progress.update(**st)
        # Une ligne d'état toutes les 2 s (progression affichée au tableau de bord)
        res = run_streaming([self.cfg.restic_bin, *args, path], on_line,
                            env={**self.env, "RESTIC_PROGRESS_FPS": "0.5"}, ok_codes=(0, 3))
        summary = {}
        for line in res.stdout.splitlines():
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("message_type") == "summary":
                summary = msg
        if res.returncode == 3:
            # Certains fichiers illisibles : instantané incomplet => on le retire
            # pour qu'il ne soit jamais pris pour une sauvegarde valide.
            sid = summary.get("snapshot_id")
            if sid:
                self.exec("forget", sid, timeout=600)
            raise OdooGuardError(
                "restic : fichiers illisibles dans le miroir, instantané incomplet supprimé\n"
                + res.stderr[-2000:], "io")
        if not summary.get("snapshot_id"):
            raise OdooGuardError("restic : aucun instantané créé", "command")
        return summary

    def forget(self, keep: dict[str, str], prune: bool = False) -> Result:
        args = ["forget", "--tag", RESTIC_TAG, "--group-by", "host,tags"]
        for k, v in keep.items():
            if v and v != "0":
                args += [f"--keep-{k}", v]
        if prune:
            args.append("--prune")
        return self.exec(*args)

    def check(self, read_data_subset: str | None = None) -> Result:
        args = ["check"]
        if read_data_subset:
            args.append(f"--read-data-subset={read_data_subset}")
        return self.exec(*args)

    def copy_to(self, dest: "Restic") -> Result:
        return run([self.cfg.restic_bin, "copy", "--from-repo", self.repo,
                    "--from-password-file", self.password_file, "--tag", RESTIC_TAG],
                   env=dest.env)

    def restore(self, snapshot: str, target: str, includes: list[str]) -> Result:
        args = ["restore", snapshot, "--target", target]
        for inc in includes:
            args += ["--include", inc]
        return self.exec(*args)

    def snapshot_at(self, when: dt.datetime) -> dict:
        """Dernier instantané pris AU PLUS TARD à l'instant demandé."""
        best = None
        for s in self.snapshots():
            t = dt.datetime.fromisoformat(s["time"].replace("Z", "+00:00"))
            if t <= when:
                best = s
        if best is None:
            raise OdooGuardError(f"aucun instantané restic antérieur à {when}", "usage")
        return best


def mirror_path(cfg: Config, source: str) -> str:
    return os.path.join(cfg.mirror_dir, source.strip("/"))


# Sortie de rsync au format numérique neutre, quelle que soit la langue du système
# (en français, rsync écrit « 8.123.456 » et « 31,20MB/s »).
RSYNC_ENV = {"LC_ALL": "C.UTF-8"}


def rsync_progress_cb(progress, transfer_total: int | None = None):
    """Fonction de rappel : lignes « rsync --info=progress2 » -> progression.

    ``transfer_total`` : volume réellement à transférer (essai à blanc préalable). Le
    pourcentage est alors calculé sur ce volume : celui de rsync, rapporté au volume
    total du dépôt, reste proche de 0 % sur une copie incrémentale.
    """
    def on_line(line: str) -> None:
        if progress is not None:
            st = parse_rsync_progress(line)
            if st:
                if transfer_total:
                    st.pop("percent", None)
                    st["bytes_total"] = transfer_total
                progress.update(**st)
    return on_line


def rsync_transfer_size(cmd: list[str], timeout: float = 1800) -> int | None:
    """Volume que rsync va réellement transférer (essai à blanc, rien n'est copié)."""
    dry = [c for c in cmd if not c.startswith("--info=")]
    dry.insert(1, "--dry-run")
    if "--stats" not in dry:
        dry.insert(1, "--stats")
    try:
        res = run(dry, env=RSYNC_ENV, timeout=timeout, check=False)
    except OdooGuardError:
        return None
    for line in res.stdout.splitlines():
        if line.lower().startswith("total transferred file size:"):
            num = re.sub(r"\D", "", line.split(":", 1)[1].split("bytes")[0])
            return int(num) if num else None
    return None


def pull_mirror(cfg: Config, progress=None) -> dict:
    """Synchronise chaque source de server01 vers le miroir local (rsync pull)."""
    agent = Agent(cfg.source)
    stats = {}
    n = len(cfg.file_sources)
    for i, src in enumerate(cfg.file_sources, 1):
        if progress is not None:
            progress.update(phase=f"1/2 synchronisation depuis server01 ({i}/{n}) : {src}")
        dest = mirror_path(cfg, src)
        os.makedirs(dest, exist_ok=True)
        # --no-inc-recursive : liste complète d'abord, donc pourcentage global fiable
        cmd = ["rsync", "-a", "--numeric-ids", "--delete", "--delete-delay", "--stats",
               "--info=progress2", "--no-inc-recursive", "--timeout=600", "-e", agent.rsync_ssh()]
        if in_business_hours() and cfg.rsync_bwlimit_day:
            cmd.append(f"--bwlimit={cfg.rsync_bwlimit_day}")
        for ex in cfg.rsync_excludes:
            cmd.append(f"--exclude={ex}")
        cmd += [agent.rsync_source(src.rstrip("/") + "/"), dest.rstrip("/") + "/"]
        total = None
        if progress is not None:
            progress.update(phase=f"1/2 analyse des changements ({i}/{n}) : {src}")
            total = rsync_transfer_size(cmd)
            progress.update(phase=f"1/2 synchronisation depuis server01 ({i}/{n}) : {src}")
        try:
            res = run_streaming(cmd, rsync_progress_cb(progress, total), env=RSYNC_ENV, ok_codes=RSYNC_OK)
        except OdooGuardError as exc:
            # Une source en échec (chemin absent...) n'empêche pas de sauvegarder les autres :
            # l'appelant crée quand même l'instantané, puis signale l'échec.
            text = str(exc)
            hint = ""
            if "(23)" in text and "Number of files: 0" in text:
                hint = (" : chemin introuvable ou illisible sur le serveur Odoo - vérifier [odoo] "
                        "file_sources (chemin des modules : addons_path de odoo.conf)")
            log.error("source %s en échec%s", src, hint or f" : {text[:300]}")
            stats[src] = {"error": (hint.lstrip(" :") or text[:300])}
            continue
        stats[src] = _rsync_stats(res.stdout)
        if res.returncode == 24:
            log.info("rsync %s : fichiers disparus pendant la copie (normal)", src)
    return stats


def failed_sources(stats: dict) -> dict:
    return {src: st["error"] for src, st in stats.items() if isinstance(st, dict) and st.get("error")}


def _rsync_stats(out: str) -> dict:
    stats = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        num = val.strip().split(" ")[0].replace(",", "").replace(".", "")
        if key in ("number of files", "number of regular files transferred",
                   "total file size", "total transferred file size") and num.isdigit():
            stats[key] = int(num)
    return stats


def restic_time(text: str) -> float | None:
    """« 2026-11-25T10:14:10.123456789+01:00 » (ou Z) -> epoch ; nanosecondes ignorées."""
    import re
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)?$", text or "")
    if not m:
        return None
    tz = m.group(2) or "+00:00"
    return dt.datetime.fromisoformat(m.group(1) + ("+00:00" if tz == "Z" else tz)).timestamp()


def snapshot_figures(history, snaps: list[dict]) -> list[dict]:
    """Complète les instantanés restic : heure (epoch), taille traitée, volume ajouté.

    restic < 0.17 n'enregistre pas de résumé dans l'instantané : les chiffres viennent
    alors de l'historique d'OdooGuard (exécution « files » dont l'étiquette = short_id).
    """
    runs = {r["label"]: r for r in history.recent(2000, component="files") if r.get("label")}
    out = []
    for s in snaps:
        s = dict(s)
        s["ts"] = restic_time(s.get("time", ""))
        summ = s.get("summary") or {}
        run = runs.get(s.get("short_id", "")[:8])
        s["size"] = summ.get("total_bytes_processed") or (run or {}).get("size_bytes")
        s["added"] = summ.get("data_added") if summ.get("data_added") is not None else (run or {}).get("added_bytes")
        out.append(s)
    return out
