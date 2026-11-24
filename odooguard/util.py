"""Fonctions utilitaires : exécution de commandes, verrous, temps, disque."""

from __future__ import annotations

import datetime as dt
import errno
import fcntl
import hashlib
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Sequence

log = logging.getLogger("odooguard")


class OdooGuardError(Exception):
    """Erreur métier : message lisible destiné à l'exploitant et aux alertes."""

    # Catégorie utilisée par les alertes : io, space, mount, command, config, lock...
    category = "error"

    def __init__(self, message: str, category: str | None = None):
        super().__init__(message)
        if category:
            self.category = category


class CommandError(OdooGuardError):
    category = "command"

    def __init__(self, cmd: Sequence[str], returncode: int, stderr: str):
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stderr = stderr
        tail = "\n".join(stderr.strip().splitlines()[-15:])
        super().__init__(f"échec ({returncode}) de « {shlex.join(self.cmd)} » :\n{tail}")
        if is_io_error_text(stderr):
            self.category = "io"
        elif is_space_error_text(stderr):
            self.category = "space"


_IO_PATTERNS = re.compile(
    r"input/output error|\bEIO\b|\[5\]|i/o error|stale file handle|remote i/o", re.I
)
_SPACE_PATTERNS = re.compile(r"no space left|\[28\]|ENOSPC|disk quota exceeded", re.I)


def is_io_error_text(text: str) -> bool:
    return bool(_IO_PATTERNS.search(text or ""))


def is_space_error_text(text: str) -> bool:
    return bool(_SPACE_PATTERNS.search(text or ""))


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str
    duration: float


def run(
    cmd: Sequence[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
    input: str | bytes | None = None,
    timeout: float | None = None,
    ok_codes: Sequence[int] = (0,),
    capture: bool = True,
    cwd: str | None = None,
) -> Result:
    """Exécute une commande sans shell (aucune interpolation possible)."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    log.debug("exec: %s", shlex.join(cmd))
    start = time.monotonic()
    text = not isinstance(input, bytes)
    try:
        proc = subprocess.run(
            list(cmd),
            env=full_env,
            input=input,
            timeout=timeout,
            text=text,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise OdooGuardError(f"commande introuvable : {cmd[0]}", "config") from exc
    except subprocess.TimeoutExpired as exc:
        raise OdooGuardError(f"délai dépassé ({timeout}s) : {shlex.join(cmd)}", "timeout") from exc
    out = proc.stdout if capture else ""
    err = proc.stderr if capture else ""
    if not text:
        out = (out or b"").decode("utf-8", "replace")
        err = (err or b"").decode("utf-8", "replace")
    res = Result(proc.returncode, out or "", err or "", time.monotonic() - start)
    if check and proc.returncode not in ok_codes:
        raise CommandError(cmd, proc.returncode, (res.stderr + "\n" + res.stdout).strip())
    return res


def run_streaming(cmd: Sequence[str], on_line, *, env: dict[str, str] | None = None,
                  ok_codes: Sequence[int] = (0,), timeout: float | None = None) -> Result:
    """Comme run(), mais appelle on_line(ligne) au fil de l'eau sur la sortie standard.

    Les lignes sont découpées sur \\n ET \\r (barres de progression de rsync).
    """
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    log.debug("exec (flux): %s", shlex.join(cmd))
    start = time.monotonic()
    try:
        proc = subprocess.Popen(list(cmd), env=full_env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0)
    except FileNotFoundError as exc:
        raise OdooGuardError(f"commande introuvable : {cmd[0]}", "config") from exc
    err_chunks: list[bytes] = []

    def _drain_err():
        for chunk in iter(lambda: proc.stderr.read(65536), b""):
            err_chunks.append(chunk)
    import threading
    t = threading.Thread(target=_drain_err, daemon=True)
    t.start()
    out_lines: list[str] = []
    buf = b""
    deadline = start + timeout if timeout else None
    for chunk in iter(lambda: proc.stdout.read(65536), b""):
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for raw in parts:
            line = raw.decode("utf-8", "replace")
            if not line.strip():
                continue
            out_lines.append(line)
            if len(out_lines) > 20000:
                del out_lines[:10000]
            try:
                on_line(line)
            except Exception as exc:  # noqa: BLE001 - la progression ne doit jamais bloquer
                log.debug("progression : %s", exc)
        if deadline and time.monotonic() > deadline:
            proc.kill()
            raise OdooGuardError(f"délai dépassé ({timeout}s) : {shlex.join(cmd)}", "timeout")
    if buf.strip():
        out_lines.append(buf.decode("utf-8", "replace"))
    rc = proc.wait()
    t.join(timeout=10)
    res = Result(rc, "\n".join(out_lines), b"".join(err_chunks).decode("utf-8", "replace"),
                 time.monotonic() - start)
    if rc not in ok_codes:
        raise CommandError(cmd, rc, (res.stderr + "\n" + res.stdout[-4000:]).strip())
    return res


@contextmanager
def file_lock(lock_dir: str, name: str, *, wait: float = 0) -> Iterator[None]:
    """Verrou exclusif (flock) : empêche deux sauvegardes concurrentes.

    Le verrou est libéré automatiquement par le noyau si le processus meurt :
    pas de verrou « orphelin » à nettoyer à la main.
    """
    os.makedirs(lock_dir, exist_ok=True)
    path = os.path.join(lock_dir, f"{name}.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o640)
    deadline = time.monotonic() + wait
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise OdooGuardError(
                        f"une opération « {name} » est déjà en cours (verrou {path})", "lock"
                    ) from None
                time.sleep(2)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        os.close(fd)


# ---------------------------------------------------------------- temps
def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone()


def parse_time(value: str, *, reference: dt.datetime | None = None) -> dt.datetime:
    """Interprète une date/heure saisie par l'exploitant (heure locale du serveur).

    Formats acceptés :
      « 2026-11-22 14:30 », « 2026-11-22 14:30:15 », « 2026-11-22T14:30:15+01:00 »,
      « hier 14:30 », « aujourd'hui 09:05 », « 14:30 » (aujourd'hui), « -2h », « -45min ».
    """
    ref = reference or now()
    v = value.strip().lower()
    m = re.fullmatch(r"-(\d+)\s*(min|m|h|j|d)", v)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"min": dt.timedelta(minutes=n), "m": dt.timedelta(minutes=n),
                 "h": dt.timedelta(hours=n), "j": dt.timedelta(days=n),
                 "d": dt.timedelta(days=n)}[unit]
        return ref - delta
    m = re.fullmatch(r"(hier|yesterday|aujourd'hui|today)?\s*(\d{1,2})[:h](\d{2})(?::(\d{2}))?", v)
    if m:
        day = ref.date()
        if m.group(1) in ("hier", "yesterday"):
            day -= dt.timedelta(days=1)
        t = dt.time(int(m.group(2)), int(m.group(3)), int(m.group(4) or 0))
        return dt.datetime.combine(day, t).astimezone()
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace(" ", "T", 1))
    except ValueError:
        raise OdooGuardError(f"date/heure non comprise : « {value} »", "usage") from None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # heure locale
    return parsed


def pg_time(value: dt.datetime) -> str:
    """Format attendu par recovery_target_time (avec fuseau explicite)."""
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


def fmt_time(ts: float | dt.datetime | None) -> str:
    if ts is None:
        return "-"
    if isinstance(ts, (int, float)):
        ts = dt.datetime.fromtimestamp(ts).astimezone()
    return ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "jamais"
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d}"
    return f"{seconds // 86400} j {seconds % 86400 // 3600} h"


def fmt_size(n: float | None) -> str:
    if n is None:
        return "-"
    for unit in ("o", "Kio", "Mio", "Gio", "Tio"):
        if abs(n) < 1024 or unit == "Tio":
            return f"{n:.1f} {unit}" if unit != "o" else f"{int(n)} o"
        n /= 1024
    return f"{n:.1f} Tio"


# ---------------------------------------------------------------- disque
def free_gb(path: str) -> float:
    return shutil.disk_usage(path).free / 1024**3


def check_free_space(path: str, min_gb: float, label: str) -> None:
    free = free_gb(path)
    if free < min_gb:
        raise OdooGuardError(
            f"espace insuffisant sur {label} ({path}) : {free:.1f} Gio libres, "
            f"minimum {min_gb:.0f} Gio",
            "space",
        )


def io_probe(directory: str, size: int = 4 * 1024 * 1024) -> float:
    """Test d'écriture réel : écrit, synchronise, relit et compare un fichier.

    Détecte les erreurs d'E/S du NAS (déjà observées) AVANT d'y copier quoi que
    ce soit, ainsi que les montages en lecture seule ou « figés ». Retourne la
    durée du test (s) ; lève OdooGuardError(category="io") en cas d'échec.
    """
    path = os.path.join(directory, f".odooguard-probe-{os.getpid()}")
    payload = os.urandom(size)
    digest = hashlib.sha256(payload).hexdigest()
    start = time.monotonic()
    try:
        with open(path, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        # Relecture sans cache local pour vérifier ce que le NAS a vraiment écrit
        fd = os.open(path, os.O_RDONLY)
        try:
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            except OSError:
                pass
            with os.fdopen(fd, "rb", closefd=False) as fh:
                back = fh.read()
        finally:
            os.close(fd)
        if hashlib.sha256(back).hexdigest() != digest:
            raise OdooGuardError(f"relecture incohérente sur {directory} (corruption ?)", "io")
    except OSError as exc:
        cat = "space" if exc.errno == errno.ENOSPC else "io"
        raise OdooGuardError(f"test d'écriture impossible sur {directory} : {exc}", cat) from exc
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return time.monotonic() - start


def in_business_hours(t: dt.datetime | None = None, start: int = 8, end: int = 18) -> bool:
    t = t or now()
    return t.weekday() < 6 and start <= t.hour < end


def ssh_base(host: str, user: str, key: str) -> list[str]:
    """Commande SSH non interactive, avec clé dédiée et hôte connu obligatoire."""
    return [
        "ssh", "-i", key,
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=4",
        f"{user}@{host}",
    ]
