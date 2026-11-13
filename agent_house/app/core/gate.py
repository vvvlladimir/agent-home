"""Event gate: the cheap deterministic prefilter that decides whether an agent (and so the LLM)
is woken at all. No model is involved here.

State change → per-agent wake rules (entity globs inside the agent's scope, from/to, above/below,
min_change) → debounce (the timer restarts while the state keeps changing) → cooldown per rule →
hourly cap per agent → `wake(...)`. Cron rules and the one-shot wakes agents schedule for
themselves pass the same hourly cap. Every wake and every drop goes to the feed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from fnmatch import fnmatchcase

from croniter import croniter

from .agents import AgentConfig, EventRule

log = logging.getLogger(__name__)
IGNORED_STATES = frozenset({"unavailable", "unknown"})
MAX_SCHEDULED = 3  # one-shot wakes an agent may have outstanding
TICK_S = 20.0


def _number(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def rule_matches(rule: EventRule, entity_id: str, old: str | None, new: str | None) -> bool:
    """Pure part of the gate: does this state change satisfy the rule (ignoring timing)?"""
    if new is None or old == new:
        return False
    if not any(fnmatchcase(entity_id, pattern) for pattern in rule.entities):
        return False
    if new in IGNORED_STATES and not (rule.to and new in rule.to):
        return False
    if rule.to is not None and new not in rule.to:
        return False
    if rule.from_ is not None and old not in rule.from_:
        return False
    if rule.above is not None or rule.below is not None:
        value = _number(new)
        if value is None:
            return False
        if rule.above is not None and not value > rule.above:
            return False
        if rule.below is not None and not value < rule.below:
            return False
    return True


class EventGate:
    def __init__(
        self,
        agents,
        feed,
        store,
        scope_of: Callable[[AgentConfig], set[str]],
        wake: Callable[..., object],
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.agents, self.feed, self.store = agents, feed, store
        self.scope_of, self.wake, self.clock, self.wall = scope_of, wake, clock, wall
        self._timers: dict[tuple[str, int], asyncio.TimerHandle] = {}
        self._pending: dict[tuple[str, int], tuple[str, str | None, str]] = {}
        self._last_rule_wake: dict[tuple[str, int], float] = {}
        self._last_value: dict[tuple[str, int, str], float] = {}
        self._wakes: dict[str, deque[float]] = {}
        self._cron_next: dict[tuple[str, int], float] = {}
        self._task: asyncio.Task | None = None

    # --- lifecycle ------------------------------------------------------------------------
    async def start(self) -> None:
        self.reload()
        self._task = asyncio.create_task(self._clock_loop(), name="event-gate")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()

    def reload(self) -> None:
        """Agents were created, edited or deleted: forget timers and cron slots, keep the caps."""
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        self._pending.clear()
        self._cron_next.clear()
        known = set(self.agents.agents)
        self._last_rule_wake = {k: v for k, v in self._last_rule_wake.items() if k[0] in known}
        self._last_value = {k: v for k, v in self._last_value.items() if k[0] in known}

    # --- state changes --------------------------------------------------------------------
    def on_state(self, entity_id: str, old: dict | None, new: dict | None) -> None:
        """HA state listener. Runs for every state change in the house: must stay cheap."""
        old_state, new_state = (old or {}).get("state"), (new or {}).get("state")
        if new_state is None or old_state == new_state:
            return
        for agent in self.agents.list():
            if not agent.enabled:
                continue
            for index, rule in enumerate(agent.wake_rules.events):
                if not rule_matches(rule, entity_id, old_state, new_state):
                    continue
                if entity_id not in self.scope_of(agent):
                    continue
                key = (agent.id, index)
                if rule.min_change is not None:
                    value, last = _number(new_state), self._last_value.get((*key, entity_id))
                    if value is None or (last is not None and abs(value - last) < rule.min_change):
                        continue
                self._pending[key] = (entity_id, old_state, new_state)
                if key in self._timers:
                    self._timers[key].cancel()  # still changing: restart the debounce
                loop = asyncio.get_running_loop()
                self._timers[key] = loop.call_later(rule.debounce_s, self._settled, key)

    def _settled(self, key: tuple[str, int]) -> None:
        self._timers.pop(key, None)
        pending = self._pending.pop(key, None)
        agent = self.agents.get(key[0])
        if pending is None or agent is None or not agent.enabled or key[1] >= len(agent.wake_rules.events):
            return
        entity_id, old_state, new_state = pending
        rule = agent.wake_rules.events[key[1]]
        now = self.clock()
        last = self._last_rule_wake.get(key)
        if last is not None and now - last < rule.cooldown_s:
            return self._drop(agent, f"cooldown ({rule.cooldown_s:g} s) for {entity_id}", entity_id)
        if rule.priority != "alarm" and not self._under_cap(agent):
            return self._drop(agent, f"hourly cap of {agent.wake_rules.max_wakes_per_hour} wakes", entity_id)
        self._last_rule_wake[key] = now
        value = _number(new_state)
        if value is not None:
            self._last_value[(*key, entity_id)] = value
        reason = f"{entity_id} changed {old_state} → {new_state}."
        if rule.reason:
            reason += f" {rule.reason}"
        self._fire(agent, "event", reason, rule.priority, entity_id)

    # --- caps -----------------------------------------------------------------------------
    def _recent(self, agent_id: str) -> deque[float]:
        wakes = self._wakes.setdefault(agent_id, deque())
        horizon = self.clock() - 3600
        while wakes and wakes[0] <= horizon:
            wakes.popleft()
        return wakes

    def wakes_last_hour(self, agent_id: str) -> int:
        return len(self._recent(agent_id))

    def _under_cap(self, agent: AgentConfig) -> bool:
        return len(self._recent(agent.id)) < agent.wake_rules.max_wakes_per_hour

    def allow_task(self, agent: AgentConfig) -> bool:
        """A task from the orchestrator counts against the same hourly cap as any wake."""
        if not self._under_cap(agent):
            return False
        self._recent(agent.id).append(self.clock())
        return True

    def _drop(self, agent: AgentConfig, why: str, entity_id: str | None = None) -> None:
        self.feed.add(
            "gate_drop",
            f"{agent.id}: not woken, {why}",
            level="debug",
            agent_id=agent.id,
            entity_id=entity_id,
        )

    def _fire(self, agent: AgentConfig, source: str, reason: str, priority: str, entity_id=None) -> None:
        self._recent(agent.id).append(self.clock())
        try:
            self.wake(agent.id, source=source, reason=reason, priority=priority, entity_id=entity_id)
        except Exception:  # noqa: BLE001 - one agent's failure must not stop the gate
            log.exception("waking %s failed", agent.id)

    # --- cron and one-shot wakes ------------------------------------------------------------
    def schedule(self, agent_id: str, delay_s: float, reason: str) -> bool:
        """An agent asks to be woken once, later. False when it already has too many outstanding."""
        count = self.store.query("SELECT COUNT(*) AS n FROM scheduled_wakes WHERE agent_id=?", (agent_id,))
        if count[0]["n"] >= MAX_SCHEDULED:
            return False
        self.store.execute(
            "INSERT INTO scheduled_wakes (agent_id, due, reason) VALUES (?, ?, ?)",
            (agent_id, self.wall() + delay_s, reason[:200]),
        )
        return True

    async def _clock_loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("event gate tick failed")
            await asyncio.sleep(min(TICK_S, self._next_due() or TICK_S))

    def _next_due(self) -> float | None:
        now = self.wall()
        due = list(self._cron_next.values())
        rows = self.store.query("SELECT MIN(due) AS due FROM scheduled_wakes")
        if rows and rows[0]["due"] is not None:
            due.append(rows[0]["due"])
        return max(0.05, min(due) - now) if due else None

    def tick(self) -> None:
        """Fire due cron rules and one-shot wakes. A slot missed while the add-on was down is
        skipped, not replayed."""
        now = self.wall()
        for agent in self.agents.list():
            for index, rule in enumerate(agent.wake_rules.cron):
                key = (agent.id, index)
                due = self._cron_next.get(key)
                if due is None or now >= due:
                    following = croniter(rule.expr, datetime.fromtimestamp(now).astimezone())
                    self._cron_next[key] = following.get_next(float)
                if due is None or now < due or not agent.enabled:
                    continue
                if not self._under_cap(agent):
                    self._drop(agent, f"hourly cap of {agent.wake_rules.max_wakes_per_hour} wakes (cron)")
                    continue
                self._fire(agent, "cron", rule.reason or f"Scheduled check ({rule.expr}).", "routine")
        for row in self.store.query("SELECT * FROM scheduled_wakes WHERE due<=? ORDER BY due", (now,)):
            self.store.execute("DELETE FROM scheduled_wakes WHERE id=?", (row["id"],))
            agent = self.agents.get(row["agent_id"])
            if agent is None or not agent.enabled:
                continue
            if not self._under_cap(agent):
                self._drop(agent, "hourly cap (scheduled wake)")
                continue
            self._fire(agent, "schedule", f"You asked to be woken: {row['reason']}", "routine")
