"""Agent council (M5): who owns what, what the agents tell each other, and the house mode.

- Ownership: every entity has exactly one owner. An entity in an area belongs to the room agent of
  that area (several agents in one area: the one that lists the entity, else the lowest id); an
  entity without an area, or in an area without an agent, belongs to the orchestrator. Only the
  owner may actuate an entity (`policy.decide`, step 2b) or have a room agent's automation act on it
  (`lifecycle`). Ownership does not depend on `enabled`, so switching an agent off moves nothing.
- Shared board: short facts with an expiry ("bedroom: went to bed 23:10"). Every wake sees its
  neighbours' fresh facts in the prompt. It replaces free agent-to-agent chat: nothing on the board
  wakes anyone, so there are no loops, and the prompt share is bounded (`MAX_FACTS_IN_PROMPT`).
- House mode: `input_select.agent_house_mode` (home / away / night / guests / work), owned by the
  orchestrator. With `auto_house_mode` on, a deterministic rule (people at home, night window) asks
  for home / away / night through the actuator like any agent action (policy, shadow, trace);
  guests and work are the owner's and are never changed automatically. Deployed automations and
  room agents read the helper, so the instant reactions between rooms run in HA, not in a model.
- Tasks down, escalation up: a mode change gives every room a task ("away: check that everything
  is off"); the run's final answer is posted to the board as a report. `ask_agent` no longer wakes
  a neighbour directly: it goes to the orchestrator, which forwards it as a task to the owner of the
  thing asked about. A task cannot ask further (depth 1). `report(severity=escalate)` lands on the
  board for the orchestrator (and in the owner's inbox, as before); when it names an entity owned
  by another agent, the orchestrator forwards it as a task to that owner through the same bounded
  path as `ask` (rate limit per agent, depth 1, never from a task or chat run). Deterministic, no
  model. Everything else waits on the board for the daily review.
- The orchestrator's wake rules are fixed (`WAKES`) and shown with the last time each fired
  (kv `orchestrator_wakes`): house mode changes, people coming and going (with `auto_house_mode`),
  the daily review, escalations.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime

from ..ha.client import MODE_HELPER, MODE_OPTIONS
from .agents import AgentConfig

log = logging.getLogger(__name__)

ORCHESTRATOR = "orchestrator"
MODE_ENTITY = MODE_HELPER
MODES = MODE_OPTIONS
OWNER_MODES = frozenset({"guests", "work"})  # set by the owner only, never left automatically
KINDS = ("fact", "task", "report", "escalation")
TEXT_MAX = 160
FACT_TTL_S = 3600.0
FACT_TTL_MAX_S = 12 * 3600.0
MAX_FACTS_IN_PROMPT = 8
MAX_POSTS_PER_RUN = 2
ASKS_PER_HOUR = 6  # per asking agent, forwarded through the orchestrator
TICK_S = 30.0
NO_FORWARD_SOURCES = frozenset({"task", "chat", "poke", "replay"})  # depth 1: these never wake another agent
WAKES = (
    ("house_mode", "the house mode changes: rooms get a task (away, night)"),
    ("person", "a person comes or goes (only with auto_house_mode): the mode is re-evaluated"),
    ("daily_review", "the daily review time: scan, digest, drafts"),
    (
        "escalation",
        "a room escalates: forwarded to the owner of an entity it names, else to the "
        "house manager (manager_enabled) or the review",
    ),
    ("manager_sweep", "every manager_sweep_min: the manager's prefilter; the model only if it finds work"),
)
WAKES_KEY = "orchestrator_wakes"
MODE_TASKS = {
    "away": "House mode is now away (nobody is home). Check your room: lights, media and heaters "
    "that should be off. Switch off what is allowed, report the rest. Finish with one short sentence.",
    "night": "House mode is now night. Check your room for lights or media left on and doors or "
    "windows left open. Act only where your tiers allow it. Finish with one short sentence.",
}


def _clean(text: object) -> str:
    return " ".join(str(text or "").split())[:TEXT_MAX]


def parse_window(text: str) -> tuple[int, int] | None:
    """`23:00-07:00` → (1380, 420) minutes of the day; None when malformed or empty."""
    try:
        start, end = (part.strip() for part in str(text).split("-"))
        minutes = [int(h) * 60 + int(m) for h, m in (p.split(":") for p in (start, end))]
    except ValueError:
        return None
    if not all(0 <= m < 1440 for m in minutes) or minutes[0] == minutes[1]:
        return None
    return minutes[0], minutes[1]


def in_window(window: tuple[int, int] | None, now: datetime) -> bool:
    if window is None:
        return False
    minute, (start, end) = now.hour * 60 + now.minute, window
    return start <= minute < end if start < end else (minute >= start or minute < end)


# --- ownership --------------------------------------------------------------------------------
class Ownership:
    def __init__(self, agents, ha) -> None:
        self.agents, self.ha = agents, ha

    def owner_of(self, entity_id: str) -> str:
        area = self.ha.registry.area_of(entity_id) if isinstance(entity_id, str) else None
        if area is None:
            return ORCHESTRATOR
        room = [a for a in self.agents.list() if a.room == area]
        if not room:
            return ORCHESTRATOR
        listing = [a for a in room if entity_id in a.entities]
        return (listing or room)[0].id  # `list()` is sorted by id

    def owned_by(self, agent_id: str) -> list[str]:
        return sorted(e for e in self.ha.states if self.owner_of(e) == agent_id)

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for entity_id in self.ha.states:
            owner = self.owner_of(entity_id)
            counts[owner] = counts.get(owner, 0) + 1
        shared: dict[str, list[str]] = {}
        for agent in self.agents.list():
            shared.setdefault(agent.room, []).append(agent.id)
        return {
            "counts": counts,
            # several agents in one area: legal, but the owner of each entity is decided as above
            "shared_rooms": {room: ids for room, ids in shared.items() if len(ids) > 1},
        }


# --- shared board -----------------------------------------------------------------------------
class Board:
    def __init__(self, store, feed, clock: Callable[[], float] = time.time) -> None:
        self.store, self.feed, self.clock = store, feed, clock

    def post(
        self,
        agent_id: str,
        text: str,
        *,
        kind: str = "fact",
        ttl_s: float = FACT_TTL_S,
        target: str | None = None,
        run_id: str | None = None,
    ) -> dict:
        if kind not in KINDS:
            raise ValueError(f"unknown board kind {kind!r}")
        text = _clean(text)
        if not text:
            raise ValueError("empty board text")
        now = self.clock()
        ttl = min(FACT_TTL_MAX_S, max(60.0, float(ttl_s)))
        cursor = self.store.execute(
            "INSERT INTO board (ts, agent_id, kind, text, expires, target, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, agent_id, kind, text, now + ttl, target, run_id),
        )
        self.feed.add(
            "board",
            f"{agent_id} [{kind}]: {text}" + (f" → {target}" if target else ""),
            level="warning" if kind == "escalation" else "info",
            agent_id=agent_id,
            run_id=run_id,
        )
        return {"id": cursor.lastrowid, "ts": now, "agent_id": agent_id, "kind": kind, "text": text,
                "expires": now + ttl, "target": target, "run_id": run_id}  # fmt: skip

    def fresh(
        self, *, exclude: str | None = None, kinds=("fact", "report"), limit: int = MAX_FACTS_IN_PROMPT
    ):
        """Unexpired entries, newest first, one per (agent, text)."""
        marks = ",".join("?" * len(kinds))
        rows = self.store.query(
            f"SELECT * FROM board WHERE expires>? AND kind IN ({marks}) AND agent_id<>? ORDER BY id DESC "
            "LIMIT 100",
            (self.clock(), *kinds, exclude or ""),
        )
        seen, out = set(), []
        for row in rows:
            key = (row["agent_id"], row["text"])
            if key not in seen:
                seen.add(key)
                out.append(row)
        return out[:limit]

    def lines(self, exclude: str | None = None) -> list[str]:
        now = self.clock()
        return [
            f"- {row['agent_id']} ({max(0, int((now - row['ts']) // 60))} min ago): {row['text']}"
            for row in self.fresh(exclude=exclude)
        ]

    def list(self, limit: int = 100, include_expired: bool = False) -> list[dict]:
        where = "" if include_expired else " WHERE expires>?"
        params = () if include_expired else (self.clock(),)
        return self.store.query(f"SELECT * FROM board{where} ORDER BY id DESC LIMIT ?", (*params, limit))

    def since(self, ts: float, kinds=KINDS) -> list[dict]:
        marks = ",".join("?" * len(kinds))
        return self.store.query(
            f"SELECT * FROM board WHERE ts>=? AND kind IN ({marks}) ORDER BY id", (ts, *kinds)
        )

    def prune(self) -> None:
        self.store.execute("DELETE FROM board WHERE expires<?", (self.clock() - 7 * 86400,))


# --- the council ------------------------------------------------------------------------------
class Council:
    """Wiring: `rt` (agent runtime), `actuator`, `gate` and `review` are set by the app factory."""

    def __init__(self, store, feed, ha, agents, safety, runtime_settings, clock=time.time) -> None:
        self.store, self.feed, self.ha, self.agents, self.safety = store, feed, ha, agents, safety
        self.settings = runtime_settings
        self.clock = clock
        self.board = Board(store, feed, clock)
        self.ownership = Ownership(agents, ha)
        self.rt = self.actuator = self.gate = self.review = self.manager = None
        self._asks: dict[str, deque[float]] = {}
        self._requested: str | None = None  # last mode asked for, so shadow mode does not repeat it
        self._task: asyncio.Task | None = None
        self._evaluate: asyncio.Task | None = None

    # --- the orchestrator as an agent of the actuator --------------------------------------
    def agent_config(self, agent_id: str) -> AgentConfig | None:
        """The orchestrator's own (pseudo) agent config: it may actuate the mode helper, nothing
        else. Shadow unless the owner set `orchestrator_mode: live` (and the global mode is live)."""
        if agent_id != ORCHESTRATOR:
            return None
        mode = getattr(self.settings, "orchestrator_mode", "shadow")
        config = AgentConfig(
            id=ORCHESTRATOR, name="Orchestrator", room="", mode="live" if mode == "live" else "shadow",
            entities=[MODE_ENTITY], entities_mode="only", max_entities=1,
        )  # fmt: skip
        if self.manager is None or not self.manager.enabled:
            return config
        # D27: with the house manager on, also the entities no room agent owns (switchable domains).
        # model_copy skips the 25-entity cap meant for room prompts; policy still checks each call.
        return config.model_copy(update={"entities": sorted(self.manager.scope())})

    # --- lifecycle ---------------------------------------------------------------------------
    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="council")

    async def stop(self) -> None:
        for task in (self._task, self._evaluate):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self._task, self._evaluate) if t), return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - one bad tick must not stop the council
                log.exception("council tick failed")

    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now().astimezone()
        self.board.prune()
        await self.evaluate_mode("time", now)
        if self.review is not None:
            await self.review.maybe_run(now)
        if self.manager is not None:
            await self.manager.tick()

    # --- house mode --------------------------------------------------------------------------
    @property
    def mode(self) -> str | None:
        state = (self.ha.states.get(MODE_ENTITY) or {}).get("state")
        return state if state in MODES else None

    def desired_mode(self, now: datetime) -> str | None:
        """home / away / night from the people in HA and the night window; None = leave it."""
        current = self.mode
        if current is None or current in OWNER_MODES:
            return None
        people = [s.get("state") for e, s in self.ha.states.items() if e.startswith("person.")]
        if not people:
            return None
        if not any(state == "home" for state in people):
            return "away"
        window = parse_window(getattr(self.settings, "night_window", "") or "")
        return "night" if in_window(window, now) else "home"

    async def evaluate_mode(self, why: str, now: datetime | None = None) -> dict | None:
        if not getattr(self.settings, "auto_house_mode", False) or self.actuator is None:
            return None
        desired = self.desired_mode(now or datetime.now().astimezone())
        if desired is None or desired == self.mode or desired == self._requested:
            return None
        self._requested = desired
        return await self.set_mode(desired, f"{why}: people and the night window say {desired}")

    async def set_mode(self, mode: str, reason: str) -> dict:
        """Ask for a mode through the actuator (policy → action log → HA), as the orchestrator."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        action = await self.actuator.request(
            ORCHESTRATOR, "input_select", "select_option", MODE_ENTITY, {"option": mode}
        )
        self.feed.add(
            "council", f"orchestrator: house mode → {mode} ({action['verdict']}). {reason}"[:300],
            agent_id=ORCHESTRATOR, entity_id=MODE_ENTITY,
        )  # fmt: skip
        return action

    def on_state(self, entity_id: str, old: dict | None, new: dict | None) -> None:
        """HA state listener: mode changes hand out tasks; people coming and going re-evaluate."""
        old_state, new_state = (old or {}).get("state"), (new or {}).get("state")
        if old_state == new_state or new_state is None:
            return
        if entity_id == MODE_ENTITY:
            self._requested = None
            if new_state in MODES:
                self.mode_changed(old_state, new_state)
        elif entity_id.startswith("person.") and getattr(self.settings, "auto_house_mode", False):
            self.wake_fired("person")
            if self._evaluate is None or self._evaluate.done():
                self._evaluate = asyncio.get_running_loop().create_task(
                    self.evaluate_mode(f"{entity_id} is {new_state}"), name="council-mode"
                )

    def mode_changed(self, old: str | None, new: str) -> list[str]:
        self.wake_fired("house_mode")
        self.board.post(ORCHESTRATOR, f"house mode is {new} (was {old or 'unknown'})", ttl_s=FACT_TTL_MAX_S)
        task = MODE_TASKS.get(new)
        if not task:
            return []
        return [
            run for agent in self.agents.list() if agent.enabled and (run := self.assign_task(agent.id, task))
        ]

    # --- tasks down -------------------------------------------------------------------------
    def assign_task(self, agent_id: str, text: str, asked_by: str | None = None) -> str | None:
        """Wake a room agent with a task from the orchestrator. None when the agent cannot run."""
        agent = self.agents.get(agent_id)
        if agent is None or not agent.enabled or self.rt is None:
            return None
        origin = f" (asked by {asked_by})" if asked_by else ""
        reason = f"Task from the orchestrator{origin}: {text}"
        if self.gate is not None and not self.gate.allow_task(agent):
            self.feed.add("council", f"{agent_id}: task not given, hourly wake cap", level="warning",
                          agent_id=agent_id)  # fmt: skip
            return None
        try:
            run_id = self.rt.wake(agent_id, source="task", reason=reason[:500])
        except Exception as err:  # noqa: BLE001 - WakeRefused and friends: the task is not given
            self.feed.add(
                "council", f"{agent_id}: task not given ({err})", level="warning", agent_id=agent_id
            )
            return None
        self.board.post(ORCHESTRATOR, text, kind="task", target=agent_id, run_id=run_id, ttl_s=3600)
        return run_id

    def task_done(self, agent_id: str, run_id: str, status: str, summary: str | None) -> None:
        """The answer of a task run is the room's report on the board."""
        text = summary or f"task ended: {status}"
        try:
            self.board.post(agent_id, text, kind="report", target=ORCHESTRATOR, run_id=run_id, ttl_s=2 * 3600)
        except ValueError:
            pass

    # --- escalation up ----------------------------------------------------------------------
    def ask(self, from_agent: str, target: str, question: str, run_id: str | None = None) -> dict:
        """`ask_agent`: a room's question goes to the orchestrator, which forwards it as a task to
        the agent asked for (or to the owner of an entity named in it). Bounded per hour."""
        if not self._may_ask(from_agent):
            return {"ok": False, "error": f"at most {ASKS_PER_HOUR} questions per hour; report it instead"}
        recipient = self._recipient(target, question)
        if recipient is None or recipient == from_agent:
            known = [a.id for a in self.agents.list() if a.enabled and a.id != from_agent]
            return {"ok": False, "error": f"unknown agent {target!r}; agents: {', '.join(known) or 'none'}"}
        self._asks[from_agent].append(self.clock())
        self.board.post(from_agent, question, kind="escalation", target=recipient, run_id=run_id, ttl_s=3600)
        if recipient == ORCHESTRATOR:
            return {"ok": True, "status": "escalated", "note": "the orchestrator and the owner will see it"}
        if self.assign_task(recipient, question, asked_by=from_agent) is None:
            return {"ok": False, "error": f"{recipient} cannot take a task right now; report it instead"}
        note = "the answer is posted to the shared board; you will see it on a later wake"
        return {"ok": True, "status": "forwarded", "to": recipient, "note": note}

    def _may_ask(self, from_agent: str) -> bool:
        """The hourly bound shared by `ask` and forwarded escalations (one budget per agent)."""
        recent = self._asks.setdefault(from_agent, deque())
        now = self.clock()
        while recent and recent[0] <= now - 3600:
            recent.popleft()
        return len(recent) < ASKS_PER_HOUR

    @staticmethod
    def _entity_words(text: str):
        for word in text.replace(",", " ").split():
            yield word.strip(".?!:;()'\"")

    def _recipient(self, target: object, question: str) -> str | None:
        """The agent asked for; else the owner of an entity named in the question; else the
        orchestrator itself when it was asked; else nobody."""
        agent = self.agents.get(target) if isinstance(target, str) else None
        if agent is not None and agent.enabled:
            return agent.id
        for word in self._entity_words(question):
            if word in self.ha.states:
                owner = self.ownership.owner_of(word)
                if owner == ORCHESTRATOR or self.agents.get(owner) is not None:
                    return owner
        return ORCHESTRATOR if target == ORCHESTRATOR else None

    def escalate(
        self, agent_id: str, summary: str, run_id: str | None = None, source: str | None = None
    ) -> dict:
        """`report(severity=escalate)`: on the board for the orchestrator; forwarded as a task to
        the owner of the first entity it names that belongs to another (enabled) room agent. Never
        from a task, chat or replay run (depth 1), and within the hourly bound of `ask`."""
        self.wake_fired("escalation")
        recipient = self._escalation_owner(agent_id, summary)
        if recipient is None:
            note = "no other owner named"
        elif source is None or source in NO_FORWARD_SOURCES:
            note, recipient = f"not forwarded from a {source or 'unknown'} run", None
        elif not self._may_ask(agent_id):
            note, recipient = f"at most {ASKS_PER_HOUR} per hour", None
        else:
            self._asks[agent_id].append(self.clock())
            note = ""
        target = recipient or ORCHESTRATOR
        self.board.post(agent_id, summary, kind="escalation", target=target, run_id=run_id, ttl_s=6 * 3600)
        if recipient is None:
            if self.manager is not None and self.manager.enabled:
                self.manager.on_escalation(agent_id, summary)
                return {"status": "on_board", "note": f"for the house manager ({note})"}
            return {"status": "on_board", "note": f"kept for the daily review ({note})"}
        if self.assign_task(recipient, summary, asked_by=agent_id) is None:
            self._asks[agent_id].pop()  # nothing was forwarded: the hourly slot is not used up
            return {"status": "on_board", "note": f"{recipient} cannot take a task now; kept for the review"}
        return {"status": "forwarded", "to": recipient}

    def _escalation_owner(self, agent_id: str, text: str) -> str | None:
        """The owner of the first entity named in `text` that belongs to another enabled room agent."""
        for word in self._entity_words(text):
            if word not in self.ha.states:
                continue
            owner = self.ownership.owner_of(word)
            agent = self.agents.get(owner) if owner not in (ORCHESTRATOR, agent_id) else None
            if agent is not None and agent.enabled:
                return owner
        return None

    # --- the orchestrator's wake rules ------------------------------------------------------
    def wake_fired(self, wake: str) -> None:
        fired = self.store.get(WAKES_KEY) or {}
        fired[wake] = self.clock()
        self.store.set(WAKES_KEY, fired)

    def wakes(self) -> list[dict]:
        fired = self.store.get(WAKES_KEY) or {}
        active = {
            "person": bool(getattr(self.settings, "auto_house_mode", False)),
            "daily_review": bool(getattr(self.settings, "daily_review_at", "") or ""),
            "manager_sweep": bool(getattr(self.settings, "manager_enabled", False))
            and int(getattr(self.settings, "manager_sweep_min", 0) or 0) > 0,
        }
        return [
            {"id": wake, "rule": rule, "active": active.get(wake, True), "last": fired.get(wake)}
            for wake, rule in WAKES
        ]

    # --- prompt -----------------------------------------------------------------------------
    def prompt_lines(self, agent_id: str) -> list[str]:
        mode = self.mode
        lines = [f"House mode: {mode or 'unknown'}"]
        facts = self.board.lines(exclude=agent_id)
        if facts:
            lines += ["Neighbours (shared board):", *facts]
        return lines

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "mode_entity": MODE_ENTITY,
            "mode_entity_present": MODE_ENTITY in self.ha.states,
            "modes": list(MODES),
            "auto_house_mode": bool(getattr(self.settings, "auto_house_mode", False)),
            "orchestrator_mode": getattr(self.settings, "orchestrator_mode", "shadow"),
            "night_window": getattr(self.settings, "night_window", ""),
            "ownership": self.ownership.summary(),
            "board": self.board.list(50),
            "orchestrator_wakes": self.wakes(),
        }
