"""Progression des sauvegardes en cours (affichée par le tableau de bord).

Chaque opération longue écrit ``<state_dir>/progress/<composant>.json`` :
phase, pourcentage, octets traités / total, vitesse, temps restant estimé.
Le fichier est remplacé atomiquement à chaque mise à jour, et supprimé à la
fin de l'opération (réussie ou non). Le service web le lit seulement.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

STALE_AFTER = 180  # sans mise à jour depuis 3 min : opération probablement interrompue


class Progress:
    def __init__(self, state_dir: str, component: str, label: str):
        self.dir = os.path.join(state_dir, "progress")
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, re.sub(r"[^a-z0-9_.-]", "_", component.lower()) + ".json")
        self.data = {"component": component, "label": label, "started_at": time.time(),
                     "phase": "", "percent": None, "bytes_done": None, "bytes_total": None,
                     "speed_bps": None, "eta_s": None, "files_done": None, "files_total": None}
        self._last = (time.time(), 0)
        self._phase_start = time.time()
        self._lock = threading.Lock()
        self._written = 0.0

    def update(self, *, phase: str | None = None, percent: float | None = None,
               bytes_done: float | None = None, bytes_total: float | None = None,
               speed_bps: float | None = None, eta_s: float | None = None, files_done: int | None = None,
               files_total: int | None = None, force: bool = False) -> None:
        with self._lock:
            now = time.time()
            if phase is not None and phase != self.data["phase"]:
                self.data.update(phase=phase, percent=None, bytes_done=None, bytes_total=None,
                                 speed_bps=None, eta_s=None, files_done=None, files_total=None)
                self._last = (now, 0)
                self._phase_start = now
                force = True
            if bytes_total:
                self.data["bytes_total"] = bytes_total
            if files_total:
                self.data["files_total"] = files_total
                self.data["files_done"] = files_done
                # Copie incrémentale : la plupart des fichiers sont déjà à destination ; le
                # pourcentage d'octets de rsync (rapporté au volume total) reste alors proche
                # de 0. La part des fichiers examinés est bien plus représentative.
                if percent is not None and files_done is not None and not self.data["bytes_total"]:
                    percent = max(percent, 100.0 * files_done / files_total)
            if bytes_done is not None:
                t0, b0 = self._last
                if speed_bps is None and now - t0 >= 5 and bytes_done >= b0:
                    inst = (bytes_done - b0) / (now - t0)
                    prev = self.data["speed_bps"]
                    speed_bps = inst if prev is None else 0.6 * prev + 0.4 * inst  # lissage
                    self._last = (now, bytes_done)
                self.data["bytes_done"] = bytes_done
                total = self.data["bytes_total"]
                if percent is None and total:
                    percent = 100.0 * bytes_done / total
            if percent is not None:
                self.data["percent"] = max(0.0, min(100.0, percent))
            if speed_bps is not None:
                self.data["speed_bps"] = speed_bps
            if eta_s is not None:
                self.data["eta_s"] = eta_s
            elif self.data["speed_bps"] and self.data["bytes_total"] and self.data["bytes_done"] is not None:
                self.data["eta_s"] = max(0.0, (self.data["bytes_total"] - self.data["bytes_done"])
                                         / self.data["speed_bps"])
            elif self.data["percent"] and self.data["percent"] >= 1:
                # Sans total connu : extrapolation à partir du pourcentage de la phase
                elapsed = now - self._phase_start
                self.data["eta_s"] = elapsed * (100 - self.data["percent"]) / self.data["percent"]
            if force or now - self._written >= 2:
                self._write(now)

    def _write(self, now: float) -> None:
        self.data["updated_at"] = now
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh)
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.path)
        self._written = now

    def close(self) -> None:
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass

    def __enter__(self):
        self._write(time.time())
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def read_all(state_dir: str) -> list[dict]:
    d = os.path.join(state_dir, "progress")
    out = []
    try:
        names = sorted(os.listdir(d))
    except FileNotFoundError:
        return out
    now = time.time()
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, n), encoding="utf-8") as fh:
                p = json.load(fh)
        except (OSError, ValueError):
            continue
        age = now - p.get("updated_at", 0)
        if age > 6 * 3600:
            continue  # reste d'une opération tuée sans nettoyage
        p["stale"] = age > STALE_AFTER
        p["elapsed_s"] = now - p.get("started_at", now)
        out.append(p)
    return out


# ------------------------------------------------------------------ analyse des sorties
RSYNC_PROGRESS_RE = re.compile(
    r"^\s*(\d[\d,.'\u00a0\u202f]*)\s+(\d{1,3})%\s+([\d.,]+)([kMGT]?B)/s\s+(\d+:\d{2}:\d{2})")
RSYNC_CHK_RE = re.compile(r"(?:to|ir)-chk=(\d+)/(\d+)")
_UNITS = {"B": 1, "kB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def parse_rsync_progress(line: str) -> dict | None:
    """Ligne de « rsync --info=progress2 » : octets, %, vitesse, temps."""
    m = RSYNC_PROGRESS_RE.match(line)
    if not m:
        return None
    # Séparateur des milliers selon la langue du système : « 8,123,456 » (anglais),
    # « 8.123.456 » (français : rsync prend le point quand la virgule est décimale)...
    done = float(re.sub(r"\D", "", m.group(1)))
    pct = float(m.group(2))
    speed = float(m.group(3).replace(",", ".")) * _UNITS.get(m.group(4), 1)
    # Pas de total déduit du pourcentage (arrondi à l'unité : trop imprécis au début)
    out = {"bytes_done": done, "percent": pct, "speed_bps": speed}
    chk = RSYNC_CHK_RE.search(line)
    if chk:  # « to-chk=reste/total » : fichiers restant à examiner (liste complète connue)
        left, total = int(chk.group(1)), int(chk.group(2))
        if total:
            out.update(files_done=total - left, files_total=total)
    return out


def parse_restic_status(line: str) -> dict | None:
    """Ligne JSON « status » de « restic backup --json »."""
    if not line.startswith("{"):
        return None
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    if msg.get("message_type") != "status":
        return None
    elapsed = msg.get("seconds_elapsed") or 0
    done = msg.get("bytes_done") or 0
    return {"percent": 100.0 * (msg.get("percent_done") or 0.0), "bytes_done": done,
            "bytes_total": msg.get("total_bytes"),
            "speed_bps": done / elapsed if elapsed else None,
            "eta_s": msg.get("seconds_remaining")}
