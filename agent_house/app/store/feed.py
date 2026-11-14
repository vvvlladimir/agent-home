"""Live feed: one stream of everything that happens, for the GUI and for debugging.

Raw state changes are kept in memory only (a real home produces hundreds per minute);
everything else (wakes, runs, actions, deploys, system events) is also persisted.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque

from ..core import clock
from .db import Store

EPHEMERAL_KINDS = {"state"}


class Feed:
    def __init__(self, store: Store, capacity: int = 1000) -> None:
        self.store = store
        self.items: deque[dict] = deque(maxlen=capacity)
        self._subscribers: set[asyncio.Queue] = set()
        rows = store.query("SELECT * FROM feed ORDER BY id DESC LIMIT 300")
        for row in reversed(rows):
            row["data"] = json.loads(row["data"]) if row["data"] else None
            self.items.append(row)
        # ids stay monotonic across restarts and across the persisted/ephemeral split
        self._next_id = max(int(clock.time() * 1000), (rows[0]["id"] + 1) if rows else 0)

    def add(
        self,
        kind: str,
        summary: str,
        *,
        level: str = "info",
        agent_id: str | None = None,
        entity_id: str | None = None,
        run_id: str | None = None,
        data: dict | None = None,
    ) -> dict:
        item = {
            "id": self._next_id,
            "ts": clock.time(),
            "kind": kind,
            "level": level,
            "summary": summary[:500],
            "agent_id": agent_id,
            "entity_id": entity_id,
            "run_id": run_id,
            "data": data,
        }
        self._next_id += 1
        self.items.append(item)
        if kind not in EPHEMERAL_KINDS:
            self.store.execute(
                "INSERT INTO feed (id, ts, kind, level, summary, agent_id, entity_id, run_id, data) "
                "VALUES (:id, :ts, :kind, :level, :summary, :agent_id, :entity_id, :run_id, :data)",
                {**item, "data": json.dumps(data) if data else None},
            )
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:  # slow browser tab: drop for that subscriber only
                pass
        return item

    def recent(self, limit: int = 100, kind: str | None = None, before: int | None = None) -> list[dict]:
        out = []
        for item in reversed(self.items):
            if kind and item["kind"] != kind:
                continue
            if before is not None and item["id"] >= before:
                continue
            out.append(item)
            if len(out) >= limit:
                break
        return out

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)
