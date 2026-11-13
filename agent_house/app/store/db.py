"""SQLite store in /data. Short synchronous queries behind one lock; WAL so a crash loses little.

Tables are added per milestone through `MIGRATIONS` (append only, never edit an applied entry).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

MIGRATIONS: list[str] = [
    # 1 — M1: key/value state (mode, kill switch) and the persisted part of the live feed
    """
    CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE feed (
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        kind TEXT NOT NULL,
        level TEXT NOT NULL DEFAULT 'info',
        summary TEXT NOT NULL,
        agent_id TEXT, entity_id TEXT, run_id TEXT,
        data TEXT
    );
    CREATE INDEX feed_ts ON feed (ts);
    """,
    # 2 — M2: benchmark runs started from the GUI (report = contracts/schemas/bench_report.schema.json)
    """
    CREATE TABLE benchmarks (
        id TEXT PRIMARY KEY,
        created TEXT NOT NULL,
        profile TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        report TEXT
    );
    """,
    # 3 — M3: agent runs with their trace, the action log (every policy decision), agent reports,
    # per-agent notes and one-shot wakes the agents scheduled for themselves
    """
    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        status TEXT NOT NULL,
        wake_source TEXT NOT NULL,
        wake_reason TEXT,
        started REAL NOT NULL,
        ended REAL,
        latency_ms INTEGER,
        steps INTEGER NOT NULL DEFAULT 0,
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        provider TEXT, profile TEXT, summary TEXT, error TEXT,
        shadow INTEGER NOT NULL DEFAULT 1,
        replay_of TEXT,
        trace TEXT NOT NULL DEFAULT '[]'
    );
    CREATE INDEX runs_agent ON runs (agent_id, id);
    CREATE TABLE actions (
        id TEXT PRIMARY KEY,
        ts REAL NOT NULL,
        run_id TEXT,
        agent_id TEXT NOT NULL,
        domain TEXT NOT NULL,
        service TEXT NOT NULL,
        entity_ids TEXT NOT NULL,
        data TEXT NOT NULL,
        tier TEXT NOT NULL,
        verdict TEXT NOT NULL,
        reason TEXT NOT NULL,
        decided_by TEXT,
        expires REAL
    );
    CREATE INDEX actions_verdict ON actions (verdict);
    CREATE TABLE inbox (
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        agent_id TEXT NOT NULL,
        run_id TEXT,
        severity TEXT NOT NULL,
        summary TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'new'
    );
    CREATE TABLE memory (
        agent_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        ts REAL NOT NULL,
        PRIMARY KEY (agent_id, key)
    );
    CREATE TABLE scheduled_wakes (
        id INTEGER PRIMARY KEY,
        agent_id TEXT NOT NULL,
        due REAL NOT NULL,
        reason TEXT NOT NULL
    );
    """,
]


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            for number, script in enumerate(MIGRATIONS, start=1):
                if number > version:
                    self._db.executescript(f"BEGIN;{script};PRAGMA user_version={number};COMMIT;")

    @property
    def schema_version(self) -> int:
        return self._db.execute("PRAGMA user_version").fetchone()[0]

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, params).fetchall()]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- key/value -----------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
