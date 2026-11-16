"""Runs: every model conversation of the orchestrator (drafts, revisions, explanations, chat, the
daily review's summary, the house manager) with its trace, as `orchestrator._Trace` writes them.
Read by `GET api/runs`, the daily review and the developer snapshot.
"""

from __future__ import annotations

import json

from . import clock

RUN_COLUMNS = (
    "id, agent_id, status, wake_source, wake_reason, started, ended, latency_ms, steps, tokens_in, "
    "tokens_out, provider, profile, summary, error, shadow, replay_of"
)


def _summary(row: dict) -> dict:
    return {**row, "shadow": bool(row["shadow"])}


class Runs:
    def __init__(self, store) -> None:
        self.store = store
        self.store.execute(
            "UPDATE runs SET status='error', error='interrupted by a restart', ended=? "
            "WHERE status IN ('queued', 'running')",
            (clock.time(),),
        )

    def list(self, agent=None, status=None, limit: int = 50, before: str | None = None) -> list[dict]:
        where, params = [], []
        for clause, value in (("agent_id=?", agent), ("status=?", status), ("id<?", before)):
            if value:
                where.append(clause)
                params.append(value)
        sql = f"SELECT {RUN_COLUMNS} FROM runs" + (" WHERE " + " AND ".join(where) if where else "")
        rows = self.store.query(sql + " ORDER BY id DESC LIMIT ?", (*params, max(1, min(limit, 200))))
        return [_summary(row) for row in rows]

    def get(self, run_id: str) -> dict | None:
        rows = self.store.query(f"SELECT {RUN_COLUMNS}, trace FROM runs WHERE id=?", (run_id,))
        return {**_summary(rows[0]), "trace": json.loads(rows[0]["trace"])} if rows else None

    def today(self) -> int:
        midnight = clock.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return self.store.query("SELECT COUNT(*) AS n FROM runs WHERE started>=?", (midnight,))[0]["n"]
