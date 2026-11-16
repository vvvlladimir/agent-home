"""The learner's journal: what happened in the house, kept on the Jetson's NVMe (D38).

SQLite (WAL), one table of events as the add-on forwards them (contracts/schemas/house_events.schema.json):
state changes with their cause, automation runs, and answers to ask notifications. A duplicate (the same
entity, moment, kind and state) is ignored, so a backfill that overlaps what came live costs nothing.
Old events are pruned after `retention_days`.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import defaultdict
from pathlib import Path

from app.core.insights import Change

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    state TEXT,
    cause TEXT NOT NULL DEFAULT 'other',
    ref TEXT,
    UNIQUE (entity_id, ts, kind, state)
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
CAUSES = frozenset({"user", "physical", "agent", "automation", "script", "other"})
KINDS = frozenset({"state", "trigger", "answer"})


class Journal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def close(self) -> None:
        with self.lock:
            self.db.close()

    # --- writing ------------------------------------------------------------------------------
    def add(self, events: list[dict]) -> tuple[int, int]:
        """(accepted, duplicates). Events are validated by the server before they get here."""
        rows = [
            (
                float(e["ts"]),
                e["kind"],
                e["entity_id"],
                e.get("state"),
                e.get("cause") or "other",
                e.get("ref"),
            )  # fmt: skip
            for e in events
        ]
        with self.lock:
            before = self.db.total_changes
            self.db.execute("BEGIN")
            self.db.executemany(
                "INSERT OR IGNORE INTO events (ts, kind, entity_id, state, cause, ref) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            self.db.execute("COMMIT")
            accepted = self.db.total_changes - before
        return accepted, len(rows) - accepted

    def prune(self, retention_days: float, now: float | None = None) -> int:
        horizon = (now or time.time()) - retention_days * 86400
        with self.lock:
            return self.db.execute("DELETE FROM events WHERE ts < ?", (horizon,)).rowcount

    def set(self, key: str, value: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))

    def get(self, key: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    # --- reading ------------------------------------------------------------------------------
    def stats(self) -> dict:
        with self.lock:
            n, first, last = self.db.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM events").fetchone()
            entities = self.db.execute("SELECT COUNT(DISTINCT entity_id) FROM events").fetchone()[0]
        try:
            size = sum(p.stat().st_size for p in self.path.parent.glob(self.path.name + "*")) / 1e6
        except OSError:
            size = None
        return {"events": n, "entities": entities, "first_ts": first, "last_ts": last,
                "size_mb": round(size, 1) if size is not None else None}  # fmt: skip

    def _rows(self, kind: str, start: float, end: float) -> list[tuple]:
        with self.lock:
            return self.db.execute(
                "SELECT ts, entity_id, state, cause, ref FROM events WHERE kind=? AND ts>=? AND ts<? "
                "ORDER BY ts, rowid",
                (kind, start, end),
            ).fetchall()

    def changes(self, start: float, end: float) -> list[Change]:
        """State changes as core/insights.py reads them from the logbook, oldest first."""
        return [
            Change(ts, entity_id, state, cause if cause in CAUSES else "other", ref)
            for ts, entity_id, state, cause, ref in self._rows("state", start, end)
            if state is not None
        ]

    def fires(self, start: float, end: float) -> dict[str, list[float]]:
        """automation entity → when it ran (core/insights.py fires_from_logbook)."""
        out: dict[str, list[float]] = defaultdict(list)
        for ts, entity_id, *_ in self._rows("trigger", start, end):
            out[entity_id].append(ts)
        return dict(out)

    def answers(self, start: float, end: float) -> list[tuple[float, str]]:
        """(when, the button's action id) of ask notifications the owner answered."""
        return [(ts, state or "") for ts, _, state, *_ in self._rows("answer", start, end)]
