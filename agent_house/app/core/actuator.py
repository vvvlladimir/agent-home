"""The single actuation path. Every service call an agent wants goes through `Actuator.request`:
policy decision → action log → (maybe) Home Assistant. Nothing else calls `ha.call_service`.

Verdicts in the action log (contracts/openapi.yaml `Verdict`): executed · shadow · denied ·
pending (T2, waits for the owner) · approved (owner said yes, sent) · expired (timed out or the
owner said no) · failed (HA returned an error).
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections import deque
from collections.abc import Callable

from .policy import Decision, decide

log = logging.getLogger(__name__)
WINDOW_S = 60.0


class ApprovalConflict(Exception):
    """The action is not waiting for an approval (unknown, already decided, expired)."""


def new_id(prefix: str) -> str:
    """Sortable by creation time, so `id < before` pages through history."""
    return f"{prefix}_{int(time.time() * 1000):011x}{secrets.token_hex(3)}"


class RateWindow:
    """Actions per entity / agent / globally in the last minute. In memory: a restart starts empty,
    which only ever means fewer than a minute of forgotten actions."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.events: deque[tuple[float, str, str]] = deque()

    def counts(self, agent_id: str, entity_id: str) -> dict:
        horizon = self.clock() - WINDOW_S
        while self.events and self.events[0][0] <= horizon:
            self.events.popleft()
        return {
            "entity": sum(1 for _, _, entity in self.events if entity == entity_id),
            "agent": sum(1 for _, agent, _ in self.events if agent == agent_id),
            "global": len(self.events),
        }

    def record(self, agent_id: str, entity_id: str) -> None:
        self.events.append((self.clock(), agent_id, entity_id))


def _row(row: dict) -> dict:
    return {**row, "entity_ids": json.loads(row["entity_ids"]), "data": json.loads(row["data"])}


class Actuator:
    def __init__(
        self,
        store,
        feed,
        ha,
        safety,
        agents,
        scope_of: Callable[[object], set[str]],
        protected: Callable[[], set[str]] = lambda: set(),
        clock: Callable[[], float] = time.time,
        owner_of: Callable[[str], str | None] = lambda entity_id: None,
        pseudo: Callable[[str], object] = lambda agent_id: None,
    ) -> None:
        """`owner_of(entity_id)`: the agent that owns the entity (council); the default owns
        nothing, so an unwired actuator denies everything. `pseudo(agent_id)`: configs of agents
        that are not room agents (the orchestrator, which may switch the house mode)."""
        self.store, self.feed, self.ha, self.safety, self.agents = store, feed, ha, safety, agents
        self.scope_of, self.protected, self.clock = scope_of, protected, clock
        self.owner_of, self.pseudo = owner_of, pseudo
        self.rates = RateWindow(time.monotonic)
        # An approval that was being sent when the add-on stopped: nobody knows if HA got it.
        self.store.execute(
            "UPDATE actions SET verdict='failed', reason='interrupted by a restart while sending' "
            "WHERE verdict='approving'"
        )

    # --- the decision ---------------------------------------------------------------------
    def _decide(self, agent_id, domain, service, entity_id, data, *, approved=False, force_shadow=False):
        agent = (self.agents.get(agent_id) or self.pseudo(agent_id)) if isinstance(agent_id, str) else None
        try:
            scope = set(self.scope_of(agent)) if agent is not None else set()
            owner = self.owner_of(entity_id) if isinstance(entity_id, str) else None
            recent = self.rates.counts(agent_id, entity_id)
            protected = set(self.protected())
            policy = self.safety.policy
            kill, mode = self.safety.kill_engaged, self.safety.mode
        except Exception:  # noqa: BLE001 - no decision without all of its inputs
            log.exception("policy inputs unavailable")
            return Decision("denied", "T3", "policy inputs unavailable (failing closed)")
        return decide(
            domain=domain,
            service=service,
            entity_id=entity_id,
            data=data,
            agent=agent.dump() if agent is not None else None,
            scope=scope,
            kill_engaged=kill,
            global_mode=mode,
            policy=policy,
            recent=recent,
            approved=approved,
            force_shadow=force_shadow,
            protected=protected,
            owner=owner,
        )

    async def request(
        self,
        agent_id: str,
        domain: str,
        service: str,
        entity_id: str,
        data: dict | None = None,
        *,
        run_id: str | None = None,
        force_shadow: bool = False,
    ) -> dict:
        """One service call on one entity. Always returns the logged Action; never raises."""
        data = data if data is not None else {}
        decision = self._decide(agent_id, domain, service, entity_id, data, force_shadow=force_shadow)
        now = self.clock()
        action = {
            "id": new_id("a"),
            "ts": now,
            "run_id": run_id,
            "agent_id": str(agent_id),
            "domain": str(domain),
            "service": str(service),
            "entity_ids": [str(entity_id)],
            "data": data if isinstance(data, dict) else {},
            "tier": decision.tier,
            "verdict": {"allow": "executed"}.get(decision.verdict, decision.verdict),
            "reason": decision.reason,
            "decided_by": None,
            "expires": None,
        }
        if decision.verdict != "denied":
            self.rates.record(action["agent_id"], action["entity_ids"][0])
        if decision.verdict == "pending":
            ttl = self.safety.policy.get("approval_ttl_s")
            action["expires"] = now + (float(ttl) if isinstance(ttl, int | float) and ttl >= 30 else 900.0)
        elif decision.verdict == "allow":
            await self._send(action, "executed")
        self._insert(action)
        self._announce(action)
        return action

    async def _send(self, action: dict, done: str) -> None:
        """The only place a service call leaves the add-on."""
        if self.safety.kill_engaged:  # re-checked: awaits happened since the decision
            action["verdict"], action["reason"] = "denied", "kill switch engaged"
            return
        try:
            await self.ha.call_service(
                action["domain"], action["service"], action["entity_ids"][0], action["data"]
            )
            action["verdict"] = done
        except Exception as err:  # noqa: BLE001 - whatever HA did, the log must say "failed"
            action["verdict"], action["reason"] = "failed", f"Home Assistant: {err}"[:300]

    # --- approvals ------------------------------------------------------------------------
    def expire_pending(self) -> int:
        """Turn overdue approvals into `expired`. Called before every read and decision."""
        now = self.clock()
        overdue = self.store.query(
            "SELECT * FROM actions WHERE verdict='pending' AND expires IS NOT NULL AND expires<=?", (now,)
        )
        for row in overdue:
            action = {**_row(row), "verdict": "expired", "reason": "approval timed out"}
            self._update(action)
            self._announce(action)
        return len(overdue)

    async def decide_approval(self, action_id: str, approve: bool) -> dict:
        self.expire_pending()
        rows = self.store.query("SELECT * FROM actions WHERE id=?", (action_id,))
        if not rows:
            raise ApprovalConflict("no such action")
        action = _row(rows[0])
        if action["verdict"] != "pending":
            raise ApprovalConflict(f"the action is {action['verdict']}, not pending")
        action["decided_by"], action["expires"] = "owner", None
        if not approve:
            action["verdict"], action["reason"] = "expired", "denied by the owner"
        else:
            # Claim the row first so a double click cannot send the call twice.
            claimed = self.store.execute(
                "UPDATE actions SET verdict='approving' WHERE id=? AND verdict='pending'", (action_id,)
            )
            if claimed.rowcount != 1:
                raise ApprovalConflict("the action is already being decided")
            # Minutes may have passed: the policy is asked again, as if the call came in now.
            decision = self._decide(
                action["agent_id"],
                action["domain"],
                action["service"],
                action["entity_ids"][0],
                action["data"],
                approved=True,
            )
            action["tier"] = decision.tier
            if decision.verdict == "allow":
                self.rates.record(action["agent_id"], action["entity_ids"][0])
                action["reason"] = decision.reason
                await self._send(action, "approved")
            elif decision.verdict == "shadow":
                action["verdict"], action["reason"] = "shadow", decision.reason
            else:
                action["verdict"] = "denied"
                action["reason"] = f"approved, but refused on re-check: {decision.reason}"
        self._update(action)
        self._announce(action)
        return action

    # --- log ------------------------------------------------------------------------------
    def _insert(self, action: dict) -> None:
        self.store.execute(
            "INSERT INTO actions (id, ts, run_id, agent_id, domain, service, entity_ids, data, tier, "
            "verdict, reason, decided_by, expires) VALUES (:id, :ts, :run_id, :agent_id, :domain, "
            ":service, :entity_ids, :data, :tier, :verdict, :reason, :decided_by, :expires)",
            {**action, "entity_ids": json.dumps(action["entity_ids"]), "data": json.dumps(action["data"])},
        )

    def _update(self, action: dict) -> None:
        self.store.execute(
            "UPDATE actions SET verdict=:verdict, reason=:reason, tier=:tier, decided_by=:decided_by, "
            "expires=:expires WHERE id=:id",
            action,
        )

    def _announce(self, action: dict) -> None:
        verdict, entity = action["verdict"], action["entity_ids"][0]
        call = f"{action['domain']}.{action['service']} {entity}"
        level = "info"
        if verdict in ("denied", "failed", "expired") or action["tier"] != "T0":
            level = "warning"  # T1 = act + notify; T2 always involves the owner
        self.feed.add(
            "approval" if verdict == "pending" or action["decided_by"] else "action",
            f"{verdict}: {call} ({action['reason']})",
            level=level,
            agent_id=action["agent_id"],
            entity_id=entity,
            run_id=action["run_id"],
            data={"action_id": action["id"], "verdict": verdict, "tier": action["tier"]},
        )

    def list(self, agent: str | None = None, verdict: str | None = None, limit: int = 100) -> list[dict]:
        self.expire_pending()
        where, params = [], []
        for column, value in (("agent_id", agent), ("verdict", verdict)):
            if value:
                where.append(f"{column}=?")
                params.append(value)
        sql = "SELECT * FROM actions" + (" WHERE " + " AND ".join(where) if where else "")
        rows = self.store.query(sql + " ORDER BY id DESC LIMIT ?", (*params, max(1, min(limit, 500))))
        return [_row(row) for row in rows]

    def pending_count(self) -> int:
        self.expire_pending()
        return self.store.query("SELECT COUNT(*) AS n FROM actions WHERE verdict='pending'")[0]["n"]
