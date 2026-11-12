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
