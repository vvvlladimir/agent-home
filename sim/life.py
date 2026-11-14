"""Make the fake house move like a real one, and let the sim panel steer it.

`Life` runs on the fake HA's own event loop, so every state change comes from one thread:
- streams: chatty sensors (power, linkquality, throughput, temperatures) on their own clocks,
  scaled by `rate` (0 = paused); `storm()` multiplies the rate for a while;
- the base house: motion blips and temperature drift, as `sim/run.py` did before;
- the owner's automations: state triggers fire and their actions run in an automation context,
  so the logbook shows "automation" as the cause (only automations whose entity is `on`);
- scenarios: scripted sequences ("arrive home", "zigbee outage", ...) from `sim/scenarios.py`;
- backfill: N days of history and logbook with the owner's routines, written quietly
  (no events reach the app) so the habit finder and "simulate against history" have data.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import logging
import random
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from sim.fake_ha import HAError, HAState
from sim.house_gen import Stream

log = logging.getLogger(__name__)

BASE_STREAMS = {
    "binary_sensor.hallway_motion": Stream("pulse", 240, states=("off", "on"), hold=20),
    "binary_sensor.living_room_motion": Stream("pulse", 300, states=("off", "on"), hold=30),
    "sensor.living_room_temperature": Stream("walk", 180, 15, 28, 0.2, 1),
    "sensor.bedroom_temperature": Stream("walk", 200, 15, 28, 0.2, 1),
    "sensor.kitchen_temperature": Stream("walk", 220, 15, 28, 0.2, 1),
    "sensor.living_room_humidity": Stream("walk", 400, 25, 80, 1, 0),
    "sensor.bedroom_humidity": Stream("walk", 400, 25, 80, 1, 0),
    "sensor.living_room_power": Stream("walk", 10, 0, 900, 4, 1),
    "sensor.living_room_illuminance": Stream("walk", 120, 0, 1500, 30, 0),
}
EVENT_LOG = 300


def _entity_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


class Life:
    def __init__(self, ha: HAState, seed: int | None = None):
        self.ha = ha
        self.rng = random.Random(seed)
        self.rate = 1.0
        self.speed = 1.0  # scenario waits are divided by this (the polygon runs them faster)
        self.storm_until = 0.0
        self.storm_factor = 1.0
        self.run_automations = True
        self.streams: dict[str, Stream] = {}
        self._heap: list[tuple[float, str]] = []
        self._pulses: dict[str, float] = {}  # pulse entity → when it falls back
        self._task: asyncio.Task | None = None
        self._scenario: asyncio.Task | None = None
        self.scenario_name: str | None = None
        self.events: deque[dict] = deque(maxlen=EVENT_LOG)
        self._stamps: deque[float] = deque(maxlen=20000)
        self.automation_runs = 0
        self._depth = 0
        self._index: dict[str, list[tuple[str, dict, list]]] = {}
        self._index_key: tuple[int, int] = (-1, -1)
        ha.listeners.append(self._on_change)

    # ---- control ----------------------------------------------------------------------------
    def set_streams(self, generated: dict[str, Stream]) -> None:
        self.streams = {**BASE_STREAMS, **generated}
        self.streams = {e: s for e, s in self.streams.items() if e in self.ha.states}
        now = time.monotonic()
        self._heap = [(now + self.rng.uniform(0, s.every), e) for e, s in self.streams.items()]
        heapq.heapify(self._heap)

    def storm(self, seconds: float = 30.0, factor: float = 20.0) -> None:
        self.storm_until, self.storm_factor = time.monotonic() + seconds, factor

    def effective_rate(self) -> float:
        storm = self.storm_factor if time.monotonic() < self.storm_until else 1.0
        return self.rate * storm

    def stats(self) -> dict:
        now = time.monotonic()
        last_min = sum(1 for t in self._stamps if now - t <= 60)
        return {
            "rate": self.rate,
            "effective_rate": round(self.effective_rate(), 2),
            "storm_left_s": max(0, round(self.storm_until - now)),
            "streams": len(self.streams),
            "events_last_min": last_min,
            "automation_runs": self.automation_runs,
            "run_automations": self.run_automations,
            "scenario": self.scenario_name,
            "speed": self.speed,
        }

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        for t in (self._task, self._scenario):
            if t:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t

    # ---- the clock ----------------------------------------------------------------------------
    async def _run(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            try:
                self.tick(time.monotonic())
            except Exception:  # a broken stream must not stop the house
                log.exception("life tick failed")

    def tick(self, now: float) -> None:
        for eid, until in list(self._pulses.items()):
            if now >= until:
                del self._pulses[eid]
                s = self.streams.get(eid)
                if s and eid in self.ha.states:
                    self.ha.physical_change(eid, s.states[0])
        rate = self.effective_rate()
        if rate <= 0:
            return
        budget = 400  # changes per tick at most, so a storm on a large house stays responsive
        while self._heap and self._heap[0][0] <= now and budget:
            _, eid = heapq.heappop(self._heap)
            s = self.streams.get(eid)
            if not s or eid not in self.ha.states:
                continue
            self.step(eid, s)
            budget -= 1
            gap = self.rng.expovariate(1.0 / s.every) / rate
            heapq.heappush(self._heap, (now + max(0.2, gap), eid))

    def step(self, eid: str, s: Stream) -> None:
        cur = self.ha.states[eid]
        if cur["state"] in ("unavailable", "unknown") and not eid.startswith("event."):
            return
        if s.kind == "walk":
            try:
                v = float(cur["state"])
            except ValueError:
                return
            v = min(s.hi, max(s.lo, v + self.rng.gauss(0, s.step)))
            self.ha.physical_change(eid, f"{v:.{s.digits}f}" if s.digits else str(round(v)))
        elif s.kind == "counter":
            with contextlib.suppress(ValueError):
                v = float(cur["state"]) + abs(self.rng.gauss(s.step, s.step / 2))
                self.ha.physical_change(eid, f"{v:.{s.digits}f}")
        elif s.kind == "battery":
            if self.rng.random() < 0.15:
                v = max(0, int(float(cur["state"])) - 1)
                self.ha.physical_change(eid, str(v))
        elif s.kind == "pulse":
            if cur["state"] == s.states[0]:
                self.ha.physical_change(eid, s.states[1])
                self._pulses[eid] = time.monotonic() + s.hold * self.rng.uniform(0.6, 1.6)
        elif s.kind == "flip":
            a, b = s.states
            if eid.startswith("event."):  # an event entity's state is the time it last fired
                attrs = {**cur["attributes"], "event_type": self.rng.choice(s.states)}
                self.ha.set_state(eid, datetime.now(UTC).isoformat(), attrs)
            else:
                self.ha.physical_change(eid, b if cur["state"] == a else a)

    # ---- event log + the owner's automations -------------------------------------------------
    def _on_change(self, eid: str, old: dict | None, new: dict | None) -> None:
        self._stamps.append(time.monotonic())
        if new is None or (old and old["state"] == new["state"]):
            return
        row = self.ha.logbook_rows[-1] if self.ha.logbook_rows else {}
        cause = "user" if row.get("context_user_id") else "physical"
        if row.get("context_event_type") == "automation_triggered":
            cause = "automation"
        if row.get("context_user_id") == "sim-agent-house-user":
            cause = "agent_house"
        self.events.append(
            {
                "ts": new["last_changed"],
                "entity_id": eid,
                "from": old["state"] if old else None,
                "to": new["state"],
                "cause": cause,
            }
        )
        if self.run_automations and self._depth < 3:
            self._depth += 1
            try:
                self._fire(eid, old, new)
            finally:
                self._depth -= 1

    def _triggers(self) -> dict[str, list[tuple[str, dict, list]]]:
        """entity → [(automation entity, state trigger, actions)], rebuilt when automations change."""
        key = (len(self.ha.automations), len(self.ha.config_writes))
        if key != self._index_key:
            by_cid = {
                s["attributes"].get("id"): e for e, s in self.ha.states.items() if e.startswith("automation.")
            }
            index: dict[str, list[tuple[str, dict, list]]] = {}
            for cid, cfg in self.ha.automations.items():
                if cid not in by_cid:
                    continue
                trigs = cfg.get("triggers") or cfg.get("trigger") or []
                for trig in trigs if isinstance(trigs, list) else [trigs]:
                    if isinstance(trig, dict) and trig.get("trigger", trig.get("platform")) == "state":
                        acts = cfg.get("actions") or cfg.get("action") or []
                        for e in _entity_ids(trig.get("entity_id")):
                            index.setdefault(e, []).append((by_cid[cid], trig, acts))
            self._index, self._index_key = index, key
        return self._index

    def _fire(self, eid: str, old: dict | None, new: dict) -> None:
        for auto, trig, actions in self._triggers().get(eid, []):
            if (self.ha.states.get(auto) or {}).get("state") != "on":
                continue  # disabled, e.g. by the migration's reversible disable
            if "to" in trig and str(trig["to"]) != new["state"]:
                continue
            if "from" in trig and (not old or str(trig["from"]) != old["state"]):
                continue
            self.run_actions(auto, actions)

    def run_actions(self, automation_eid: str, actions: list) -> None:
        """Run the `domain.service` actions of an automation in its own context (others are skipped)."""
        ctx = {"id": f"SIMAUTO{self.rng.getrandbits(64):016X}", "parent_id": None, "user_id": None}
        self.ha._origins[ctx["id"]] = {
            "context_event_type": "automation_triggered",
            "context_domain": "automation",
            "context_entity_id": automation_eid,
            "context_name": automation_eid,
        }
        self.ha.fire_event("automation_triggered", {"name": automation_eid, "entity_id": automation_eid})
        cur = self.ha.states.get(automation_eid)
        if cur:
            cur["attributes"]["last_triggered"] = datetime.now(UTC).isoformat()
        self.automation_runs += 1
        for act in actions if isinstance(actions, list) else [actions]:
            if not isinstance(act, dict):
                continue
            svc = act.get("action") or act.get("service")
            if not isinstance(svc, str) or "." not in svc:
                continue
            domain, service = svc.split(".", 1)
            data = dict(act.get("data") or {})
            ids = _entity_ids((act.get("target") or {}).get("entity_id")) or _entity_ids(
                data.get("entity_id")
            )
            data["entity_id"] = ids
            with contextlib.suppress(HAError):
                self.ha.call_service(domain, service, data, context=ctx)

    # ---- history -----------------------------------------------------------------------------
    def backfill(self, days: int, owner_tracker: str | None = None) -> dict:
        """Write `days` of the owner's routine into history + logbook, quietly, then restore now."""
        days = max(0, min(int(days), 30))
        ha, rng = self.ha, random.Random(1234)
        keep = {e: dict(s) for e, s in ha.states.items()}
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
        ops: list[tuple[datetime, str, str, str]] = []  # (ts, cause, entity, state)

        def at(day: datetime, hh: float, jitter_min: float = 15) -> datetime:
            return day + timedelta(hours=hh, minutes=rng.gauss(0, jitter_min / 2))

        trackers = [owner_tracker] if owner_tracker and owner_tracker in ha.states else []
        for d in range(days):
            day = start + timedelta(days=d)
            workday = day.weekday() < 5
            wake = 6.75 if workday else 8.5
            ops += [
                (at(day, wake), "user", "light.kitchen_ceiling", "on"),
                (at(day, wake + 0.1, 5), "physical", "binary_sensor.hallway_motion", "on"),
                (at(day, wake + 0.12, 5), "physical", "binary_sensor.hallway_motion", "off"),
                (at(day, wake + 0.9), "user", "light.kitchen_ceiling", "off"),
            ]
            if workday:
                leave, back = at(day, 8.25), at(day, 17.75, 40)
                ops += [
                    (leave, "physical", "binary_sensor.front_door_contact", "on"),
                    (leave + timedelta(seconds=40), "physical", "binary_sensor.front_door_contact", "off"),
                    (leave + timedelta(minutes=2), "user", "lock.front_door", "locked"),
                    (leave + timedelta(minutes=5), "physical", "person.owner", "not_home"),
                    (back, "physical", "person.owner", "home"),
                    (back + timedelta(minutes=1), "physical", "binary_sensor.front_door_contact", "on"),
                    (
                        back + timedelta(minutes=1, seconds=30),
                        "physical",
                        "binary_sensor.front_door_contact",
                        "off",
                    ),
                ]
                ops += [
                    (t, "physical", tr, st)
                    for tr in trackers
                    for t, st in ((leave + timedelta(minutes=4), "not_home"), (back, "home"))
                ]
            ops += [
                (at(day, 19.0, 30), "user", "light.living_room_lamp", "on"),
                (at(day, 19.25, 30), "user", "media_player.living_room_tv", "on"),
                (at(day, 20.0, 2), "automation", "cover.living_room_blinds", "closed"),
                (at(day, 22.4, 20), "user", "media_player.living_room_tv", "off"),
                (at(day, 22.5, 20), "user", "light.living_room_lamp", "off"),
                (at(day, 22.6, 20), "user", "light.bedroom_lamp", "on"),
                (at(day, 23.1, 20), "user", "light.bedroom_lamp", "off"),
                (at(day, 23.2, 10), "user", "lock.front_door", "locked"),
            ]
            for _ in range(rng.randint(15, 35)):  # moving around the house
                t = at(day, rng.uniform(wake, 23), 0)
                eid = rng.choice(["binary_sensor.hallway_motion", "binary_sensor.living_room_motion"])
                ops += [
                    (t, "physical", eid, "on"),
                    (t + timedelta(seconds=rng.randint(20, 120)), "physical", eid, "off"),
                ]
            for h in range(24):  # hourly climate
                for eid, base in (
                    ("sensor.living_room_temperature", 21.0),
                    ("sensor.bedroom_temperature", 19.0),
                ):
                    v = base + 1.2 * (1 if 7 <= h <= 22 else -1) * rng.random() + rng.gauss(0, 0.2)
                    ops.append(
                        (day + timedelta(hours=h, minutes=rng.randint(0, 59)), "physical", eid, f"{v:.1f}")
                    )
        ops = [o for o in ops if o[2] in ha.states and o[0] < datetime.now(UTC) - timedelta(minutes=5)]
        ops.sort(key=lambda o: o[0])
        touched = {o[2] for o in ops}
        before = (start - timedelta(seconds=1)).isoformat()
        for eid in touched:  # the state before the backfill starts, so every write lands in order
            ha.history.pop(eid, None)
            ha.states[eid] = {**keep[eid], "last_changed": before, "last_updated": before}
        ha.logbook_rows = [r for r in ha.logbook_rows if r.get("entity_id") not in touched]
        quiet, ha.quiet = ha.quiet, True
        try:
            automation = next((e for e, s in ha.states.items() if e.startswith("automation.evening")), None)
            for ts, cause, eid, state in ops:
                if cause == "user":
                    ha.user_change(eid, state, ts=ts)
                elif cause == "automation" and automation:
                    ha.run_automation(automation, [(eid, state)], ts=ts)
                else:
                    ha.physical_change(eid, state, ts=ts)
            now = datetime.now(UTC).isoformat()
            for eid in touched:
                rows = ha.history.setdefault(eid, [])
                if ha.states[eid]["state"] != keep[eid]["state"]:  # back to the live state, reported now
                    rows.append(ha.states[eid])
                    ha.states[eid] = {**keep[eid], "last_changed": now, "last_updated": now}
        finally:
            ha.quiet = quiet
        for rows in ha.history.values():
            rows.sort(key=lambda r: r["last_updated"])
        ha.logbook_rows.sort(key=lambda r: r["when"])
        rows = sum(1 for r in ha.logbook_rows if r.get("entity_id") in touched)
        return {"days": days, "changes": len(ops), "logbook_rows": rows}

    # ---- scenarios -----------------------------------------------------------------------------
    def play(self, name: str, steps: list[dict]) -> None:
        if self._scenario and not self._scenario.done():
            self._scenario.cancel()
        self.scenario_name = name
        self._scenario = asyncio.get_running_loop().create_task(self._play(name, steps))

    async def _play(self, name: str, steps: list[dict]) -> None:
        try:
            for st in steps:
                if st.get("wait"):
                    await asyncio.sleep(float(st["wait"]) / max(self.speed, 0.01))
                    continue
                self.apply(st)
        finally:
            if self.scenario_name == name:
                self.scenario_name = None

    def scenario_running(self) -> bool:
        return bool(self._scenario and not self._scenario.done())

    def apply(self, st: dict) -> None:
        """One step: {"entity": .., "state": .., "cause": user|physical|automation, "attributes": {..}},
        {"many": "<prefix or domain.*>", "state": ..}, {"fault": ...} is handled by the caller."""
        ha = self.ha
        targets = [st["entity"]] if st.get("entity") else []
        if st.get("many"):
            pat = st["many"]
            targets = [e for e in ha.states if (e.startswith(pat[:-1]) if pat.endswith("*") else pat in e)]
        for eid in targets:
            if eid not in ha.states:
                continue
            if st.get("attributes"):
                ha.states[eid]["attributes"].update(st["attributes"])
            state = st.get("state")
            if state == "$restore":
                state = st.get("_restore", {}).get(eid)
                if state is None:
                    continue
            cause = st.get("cause", "physical")
            if cause == "user":
                ha.user_change(eid, state)
            elif cause == "automation":
                ha.run_automation(st.get("automation", "automation.sim_scenario"), [(eid, state)])
            else:
                ha.physical_change(eid, state)
