"""The council (M5, D40): who may act on what, and the house mode.

- Ownership: every entity has exactly one owner, and since room agents are gone (D40) that is the
  orchestrator, for every entity but the network pairing switches (`NEVER_OWNED`: nobody). Only the
  owner may actuate an entity (`policy.decide`, step 2b); the learner acts for the orchestrator
  (`Actuator.delegates`), nobody else can act at all.
- Actors: the orchestrator (the house mode helper; with the house manager on, the switchable
  devices `Manager.scope` lists) and the learner (the devices of its fresh model's live targets).
  Each is shadow unless its own mode (`orchestrator_mode` / `learner_mode`) and the global mode
  are live; tiers, the kill switch and the rate limits apply to both like to anyone.
- House mode: `input_select.agent_house_mode` (home / away / night / guests / work). With
  `auto_house_mode` on, a deterministic rule (people at home, night window) asks for home / away /
  night through the actuator as the orchestrator (policy, shadow, trace); guests and work are the
  owner's and are never changed automatically. Deployed automations read the helper, so the
  instant reactions between rooms run in HA, not in a model.
- The orchestrator's wake rules are fixed (`WAKES`) and shown with the last time each fired
  (kv `orchestrator_wakes`): people coming and going (with `auto_house_mode`), the daily review,
  the house manager's sweep.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from ..ha.client import MODE_HELPER, MODE_OPTIONS
from . import clock
from .entities import NEVER_OWNED
from .learner import LIVE_DOMAINS

log = logging.getLogger(__name__)

ORCHESTRATOR = "orchestrator"
LEARNER = "learner"  # D38: the Jetson learner's live suggestions (core/learner.py)
MODE_ENTITY = MODE_HELPER
MODES = MODE_OPTIONS
OWNER_MODES = frozenset({"guests", "work"})  # set by the owner only, never left automatically
TICK_S = 30.0
ACTIONS_PER_MINUTE = 6  # per actor, on top of the policy's per-entity and global limits
WAKES = (
    ("person", "a person comes or goes (only with auto_house_mode): the mode is re-evaluated"),
    ("daily_review", "the daily review time: scan, digest, drafts"),
    ("manager_sweep", "every manager_sweep_min: the manager's prefilter; the model only if it finds work"),
)
WAKES_KEY = "orchestrator_wakes"


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
    """Every entity belongs to the orchestrator (D40: no room agents), except network pairing
    switches, which belong to nobody: nothing of ours may ever switch them."""

    def __init__(self, ha) -> None:
        self.ha = ha

    @staticmethod
    def owner_of(entity_id: str) -> str | None:
        if not isinstance(entity_id, str) or "." not in entity_id or NEVER_OWNED.search(entity_id):
            return None
        return ORCHESTRATOR

    def owned_by(self, agent_id: str) -> list[str]:
        return sorted(e for e in self.ha.states if self.owner_of(e) == agent_id)

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for entity_id in self.ha.states:
            owner = self.owner_of(entity_id) or "nobody"
            counts[owner] = counts.get(owner, 0) + 1
        return {"counts": counts}


# --- the council ------------------------------------------------------------------------------
class Council:
    """Wiring: `actuator`, `review`, `manager`, `unusual` and `learner` are set by the app factory."""

    def __init__(self, store, feed, ha, safety, runtime_settings, clock=time.time) -> None:
        self.store, self.feed, self.ha, self.safety = store, feed, ha, safety
        self.settings = runtime_settings
        self.clock = clock
        self.ownership = Ownership(ha)
        self.actuator = self.review = self.manager = self.unusual = None
        self.learner = None  # D38: core/learner.py
        self._requested: str | None = None  # last mode asked for, so shadow mode does not repeat it
        self._task: asyncio.Task | None = None
        self._evaluate: asyncio.Task | None = None

    # --- the actors the actuator knows ---------------------------------------------------------
    def actor(self, agent_id: object) -> dict | None:
        """The policy's view of an actor (`policy.decide` reads id, enabled, mode, tier_overrides,
        limits; `entities` is its allowlist), or None: unknown ids are denied.

        - `orchestrator`: the house mode helper; with the house manager on, also the switchable
          devices `Manager.scope` lists (D27). Shadow unless `orchestrator_mode: live`.
        - `learner` (D38/D39): the devices among the live targets of its fresh model, never a
          helper. Shadow unless `learner_mode: live`. Only when a learner is configured.
        The global mode must be live too; tiers, ownership and the kill switch apply to both."""
        if agent_id == LEARNER:
            if self.learner is None or not self.learner.configured:
                return None
            live = getattr(self.settings, "learner_mode", "shadow") == "live"
            # the link filters too (LearnerLink.targets); a helper never, whatever reaches here
            entities = [e for e in self.learner.targets() if e.split(".", 1)[0] in LIVE_DOMAINS]
        elif agent_id == ORCHESTRATOR:
            live = getattr(self.settings, "orchestrator_mode", "shadow") == "live"
            manager = self.manager is not None and self.manager.enabled
            entities = sorted(self.manager.scope()) if manager else [MODE_ENTITY]
        else:
            return None
        return {"id": agent_id, "enabled": True, "mode": "live" if live else "shadow", "tier_overrides": {},
                "limits": {"max_actions_per_minute": ACTIONS_PER_MINUTE}, "entities": entities}  # fmt: skip

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
        now = now or clock.local()
        await self.evaluate_mode("time", now)
        if self.review is not None:
            self.review.maybe_first_look()
            await self.review.maybe_run(now)
        if self.unusual is not None:
            self.unusual.check()  # D35: a device in a state it is almost never in now
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
        desired = self.desired_mode(now or clock.local())
        # compare with what we last asked for: in shadow the helper never changes, and comparing with
        # it hid every way back (away was recorded, home after the trip never was; stories 2026-11-15)
        if desired is None or desired == (self._requested or self.mode):
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
        """HA state listener: people coming and going re-evaluate the mode (`auto_house_mode`)."""
        old_state, new_state = (old or {}).get("state"), (new or {}).get("state")
        if old_state == new_state or new_state is None:
            return
        if entity_id == MODE_ENTITY:
            self._requested = None
        elif entity_id.startswith("person.") and getattr(self.settings, "auto_house_mode", False):
            self.wake_fired("person")
            if self._evaluate is None or self._evaluate.done():
                self._evaluate = asyncio.get_running_loop().create_task(
                    self.evaluate_mode(f"{entity_id} is {new_state}"), name="council-mode"
                )

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

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "mode_entity": MODE_ENTITY,
            "mode_entity_present": MODE_ENTITY in self.ha.states,
            "modes": list(MODES),
            "auto_house_mode": bool(getattr(self.settings, "auto_house_mode", False)),
            "orchestrator_mode": getattr(self.settings, "orchestrator_mode", "shadow"),
            "learner_mode": getattr(self.settings, "learner_mode", "shadow"),
            "night_window": getattr(self.settings, "night_window", ""),
            "ownership": self.ownership.summary(),
            "orchestrator_wakes": self.wakes(),
        }
