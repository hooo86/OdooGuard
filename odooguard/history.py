"""Historique SQLite : exécutions, mesures et journal d'audit.

L'historique sert au tableau de bord et aux alertes, mais il n'est jamais la
source de vérité sur la validité d'une sauvegarde : celle-ci vient toujours
de pgBackRest (``info``) et de restic (``snapshots``), qui n'enregistrent une
sauvegarde qu'une fois terminée et cohérente.
"""

from __future__ import annotations

import getpass
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    component   TEXT NOT NULL,          -- db, files, copy:<dest>, verify, restore...
    kind        TEXT NOT NULL,          -- full, diff, incr, snapshot, ...
    started_at  REAL NOT NULL,
    ended_at    REAL,
    status      TEXT NOT NULL,          -- running, success, failed
    category    TEXT,                   -- io, space, mount, command... si échec
    size_bytes  INTEGER,                -- volume de la sauvegarde (logique)
    added_bytes INTEGER,                -- volume réellement ajouté au dépôt
    label       TEXT,                   -- étiquette pgBackRest / identifiant restic
    message     TEXT,
    details     TEXT                    -- JSON libre
);
CREATE INDEX IF NOT EXISTS runs_component ON runs(component, started_at);

CREATE TABLE IF NOT EXISTS metrics (
    ts     REAL NOT NULL,
    name   TEXT NOT NULL,
    value  REAL
);
CREATE INDEX IF NOT EXISTS metrics_name ON metrics(name, ts);

CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    actor    TEXT NOT NULL,
    action   TEXT NOT NULL,
    target   TEXT,
    outcome  TEXT,
    details  TEXT
);
"""


class History:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------ exécutions
    def start(self, component: str, kind: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO runs(component, kind, started_at, status) VALUES (?,?,?,?)",
                (component, kind, time.time(), "running"),
            )
            return int(cur.lastrowid)

    def finish(self, run_id: int, status: str, *, message: str = "", category: str | None = None,
               size: int | None = None, added: int | None = None, label: str | None = None,
               details: dict[str, Any] | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET ended_at=?, status=?, message=?, category=?, size_bytes=?,"
                " added_bytes=?, label=?, details=? WHERE id=?",
                (time.time(), status, message[:4000], category, size, added, label,
                 json.dumps(details or {}, default=str), run_id),
            )

    def mark_interrupted(self, max_age: float = 6 * 3600) -> int:
        """Les exécutions « running » trop anciennes (processus tué) passent en échec."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE runs SET status='failed', category='interrupted', ended_at=?,"
                " message='exécution interrompue (processus arrêté)'"
                " WHERE status='running' AND started_at < ?",
                (time.time(), time.time() - max_age),
            )
            return cur.rowcount

    def last(self, component: str, status: str | None = None, kind: str | None = None) -> dict | None:
        q = "SELECT * FROM runs WHERE component=?"
        args: list[Any] = [component]
        if status:
            q += " AND status=?"
            args.append(status)
        if kind:
            q += " AND kind=?"
            args.append(kind)
        q += " ORDER BY started_at DESC LIMIT 1"
        with self._conn() as c:
            row = c.execute(q, args).fetchone()
            return dict(row) if row else None

    def recent(self, limit: int = 50, component: str | None = None) -> list[dict]:
        q = "SELECT * FROM runs"
        args: list[Any] = []
        if component:
            q += " WHERE component LIKE ?"
            args.append(component)
        q += " ORDER BY started_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    # ------------------------------------------------------------ mesures
    def metric(self, name: str, value: float, ts: float | None = None) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO metrics(ts, name, value) VALUES (?,?,?)",
                      (ts or time.time(), name, value))

    def metric_max(self, name: str, since: float) -> tuple[float | None, int]:
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(value), COUNT(*) FROM metrics WHERE name=? AND ts>=?", (name, since)
            ).fetchone()
            return (row[0], row[1])

    def metric_series(self, name: str, since: float) -> list[tuple[float, float]]:
        with self._conn() as c:
            return [(r[0], r[1]) for r in c.execute(
                "SELECT ts, value FROM metrics WHERE name=? AND ts>=? ORDER BY ts", (name, since))]

    def purge_metrics(self, older_than: float) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM metrics WHERE ts < ?", (older_than,))

    # ------------------------------------------------------------ audit
    def audit(self, action: str, target: str, outcome: str, details: dict | None = None) -> None:
        actor = os.environ.get("SUDO_USER") or getpass.getuser()
        with self._conn() as c:
            c.execute(
                "INSERT INTO audit(ts, actor, action, target, outcome, details) VALUES (?,?,?,?,?,?)",
                (time.time(), actor, action, target, outcome, json.dumps(details or {}, default=str)),
            )
