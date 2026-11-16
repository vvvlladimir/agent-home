"""The owner's inbox: reports of the orchestrator (daily review, first look, the house manager, the
budget notice) and the house's own notes (unusual states). Every entry is also in the live feed.
An alarm about an entity that already has an open alarm updates that one instead of adding another.
"""

from __future__ import annotations

import re

from . import clock

ALARM_MERGE_S = 1800.0  # a second alarm about the same entity within this updates the first
ENTITY_ID = re.compile(
    r"\b(?:binary_sensor|sensor|lock|alarm_control_panel|cover|valve|siren|light|switch)\.[a-z0-9_]+\b"
)


class Inbox:
    def __init__(self, store, feed) -> None:
        self.store, self.feed = store, feed

    def add(
        self, agent_id: str, run_id: str | None, severity: str, summary: str, entity_id: str | None = None
    ) -> None:
        """`entity_id`: the entity the note is about, so the feed links it even when the text names
        it in words ("Bedroom window") rather than by id."""
        same = self._open_alarm_about(summary) if severity == "alarm" else None
        if same is not None:  # QA-001: one alarm per thing, kept up to date, not one per report
            self.store.execute(
                "UPDATE inbox SET ts=?, agent_id=?, run_id=?, summary=? WHERE id=?",
                (clock.time(), agent_id, run_id, summary, same),
            )
        else:
            self.store.execute(
                "INSERT INTO inbox (ts, agent_id, run_id, severity, summary) VALUES (?, ?, ?, ?, ?)",
                (clock.time(), agent_id, run_id, severity, summary),
            )
        self.feed.add(
            "report",
            f"{agent_id}: {summary}"[:300],
            level="info" if severity == "info" else "warning",
            agent_id=agent_id,
            entity_id=entity_id,
            run_id=run_id,
            data={"severity": severity},
        )

    def _open_alarm_about(self, summary: str) -> int | None:
        """An unhandled alarm of the last `ALARM_MERGE_S` that names one of the same entities."""
        named = set(ENTITY_ID.findall(summary))
        if not named:
            return None
        rows = self.store.query(
            "SELECT id, summary FROM inbox WHERE severity='alarm' AND status='new' AND ts>=? "
            "ORDER BY id DESC",
            (clock.time() - ALARM_MERGE_S,),
        )
        return next((r["id"] for r in rows if named & set(ENTITY_ID.findall(r["summary"]))), None)

    def list(self, limit: int = 100) -> list[dict]:
        return self.store.query("SELECT * FROM inbox ORDER BY id DESC LIMIT ?", (limit,))
