"""Event gate: the deterministic prefilter in front of the model."""

from __future__ import annotations

import asyncio

import pytest

from app.core.agents import EventRule
from app.core.gate import MAX_SCHEDULED, EventGate, rule_matches
from tests.helpers import AGENT, make_stack

MOTION = "binary_sensor.room_motion"
LUX = "sensor.room_illuminance"


def rule(**kw) -> EventRule:
    return EventRule(**{"entities": [MOTION], **kw})


def test_rule_matching_is_pure_and_strict():
    assert rule_matches(rule(), MOTION, "off", "on")
    assert not rule_matches(rule(), MOTION, "on", "on"), "attribute-only updates never wake"
    assert not rule_matches(rule(), "binary_sensor.other", "off", "on")
    assert rule_matches(rule(entities=["binary_sensor.*motion"]), MOTION, "off", "on")
    assert not rule_matches(rule(to=["on"]), MOTION, "on", "off")
    assert not rule_matches(rule(**{"from": ["off"]}), MOTION, "unavailable", "on")
    assert not rule_matches(rule(), MOTION, "on", "unavailable"), "a sensor dropping out is not an event"
    assert rule_matches(rule(to=["unavailable"]), MOTION, "on", "unavailable"), "unless the rule asks for it"
    assert not rule_matches(rule(), MOTION, "on", None)
    lux = {"entities": [LUX]}
    assert rule_matches(rule(**lux, below=30), LUX, "40", "12")
    assert not rule_matches(rule(**lux, below=30), LUX, "12", "40")
    assert not rule_matches(rule(**lux, above=30, below=100), LUX, "12", "150")
    assert not rule_matches(rule(**lux, above=30), LUX, "12", "bright")


class Gate:
    """A gate on a fake clock that records wakes instead of running agents."""

    def __init__(self, tmp_path, wake_rules: dict, **agent) -> None:
        self.stack = make_stack(tmp_path, agents=[{**AGENT, **agent, "wake_rules": wake_rules}])
        self.now, self.wall = [1000.0], [1_800_000_000.0]
        self.wakes: list[dict] = []
        self.gate = EventGate(
            self.stack.agents,
            self.stack.feed,
            self.stack.store,
            self.stack.rt.scope,
            lambda agent_id, **kw: self.wakes.append({"agent": agent_id, **kw}),
            clock=lambda: self.now[0],
            wall=lambda: self.wall[0],
        )

    def change(self, entity: str, old: str, new: str) -> None:
        self.gate.on_state(entity, {"state": old}, {"state": new})

    def drops(self) -> list[str]:
        return [item["summary"] for item in self.stack.feed.recent(kind="gate_drop")]


async def test_debounce_restarts_while_the_state_keeps_changing(tmp_path):
    g = Gate(tmp_path, {"events": [{"entities": [MOTION], "debounce_s": 0.05, "reason": "Someone moved."}]})
    for old, new in (("off", "on"), ("on", "off"), ("off", "on")):
        g.change(MOTION, old, new)
        await asyncio.sleep(0.02)
    assert g.wakes == [], "still inside the debounce window"
    await asyncio.sleep(0.08)
    assert len(g.wakes) == 1
    wake = g.wakes[0]
    assert wake["source"] == "event" and wake["entity_id"] == MOTION
    assert wake["reason"] == f"{MOTION} changed off → on. Someone moved."


async def test_cooldown_per_rule(tmp_path):
    g = Gate(tmp_path, {"events": [{"entities": [MOTION], "to": ["on"], "debounce_s": 0, "cooldown_s": 60}]})
    for _ in range(3):
        g.change(MOTION, "off", "on")
        await asyncio.sleep(0.01)
    assert len(g.wakes) == 1 and len(g.drops()) == 2 and "cooldown" in g.drops()[0]
    g.now[0] += 61
    g.change(MOTION, "off", "on")
    await asyncio.sleep(0.01)
    assert len(g.wakes) == 2


async def test_hourly_cap_and_alarm_priority(tmp_path):
    rules = {
        "events": [
            {"entities": [MOTION], "debounce_s": 0, "cooldown_s": 0},
            {"entities": [LUX], "debounce_s": 0, "cooldown_s": 0, "priority": "alarm"},
        ],
        "max_wakes_per_hour": 2,
    }
    g = Gate(tmp_path, rules)
    for _ in range(4):
        g.change(MOTION, "off", "on")
        await asyncio.sleep(0.01)
    assert len(g.wakes) == 2 and "hourly cap" in g.drops()[0]
    assert g.gate.wakes_last_hour("room") == 2
    g.change(LUX, "4", "900")
    await asyncio.sleep(0.01)
    assert g.wakes[-1]["priority"] == "alarm", "an alarm rule is not held back by the routine cap"
    g.now[0] += 3601
    g.change(MOTION, "off", "on")
    await asyncio.sleep(0.01)
    assert len(g.wakes) == 4


async def test_only_entities_in_scope_wake_and_disabled_agents_sleep(tmp_path):
    g = Gate(tmp_path, {"events": [{"entities": ["binary_sensor.*"], "debounce_s": 0}]})
    g.stack.ha.add("binary_sensor.other_room_motion", "off", area="elsewhere")
    g.change("binary_sensor.other_room_motion", "off", "on")
    await asyncio.sleep(0.01)
    assert g.wakes == []
    g.stack.agents.save(g.stack.agents.get("room").model_copy(update={"enabled": False}))
    g.change(MOTION, "off", "on")
    await asyncio.sleep(0.01)
    assert g.wakes == []


async def test_min_change_is_measured_from_the_last_wake(tmp_path):
    g = Gate(tmp_path, {"events": [{"entities": [LUX], "min_change": 50, "debounce_s": 0, "cooldown_s": 0}]})
    for old, new in (("100", "120"), ("120", "140"), ("140", "175"), ("175", "180")):
        g.change(LUX, old, new)
        await asyncio.sleep(0.01)
    assert len(g.wakes) == 2, "100→120 (first value) wakes, then only 175 is ≥ 50 away from 120"


async def test_cron_fires_once_per_slot_and_skips_missed_slots(tmp_path):
    g = Gate(tmp_path, {"cron": [{"expr": "*/5 * * * *", "reason": "Regular check."}]})
    g.gate.tick()
    assert g.wakes == [], "the first tick only computes the next slot"
    g.wall[0] += 301
    g.gate.tick()
    g.gate.tick()
    assert [w["source"] for w in g.wakes] == ["cron"] and g.wakes[0]["reason"] == "Regular check."
    g.wall[0] += 3600  # the add-on was asleep for an hour: one wake, not twelve
    g.gate.tick()
    assert len(g.wakes) == 2


async def test_scheduled_wakes_are_persisted_capped_and_fire_once(tmp_path):
    g = Gate(tmp_path, {})
    assert all(g.gate.schedule("room", 600, f"check {i}") for i in range(MAX_SCHEDULED))
    assert not g.gate.schedule("room", 600, "one too many")
    g.gate.tick()
    assert g.wakes == []
    g.wall[0] += 601
    g.gate.tick()
    g.gate.tick()
    assert [w["source"] for w in g.wakes] == ["schedule"] * MAX_SCHEDULED
    assert "check 0" in g.wakes[0]["reason"]


async def test_reload_forgets_timers_of_changed_agents(tmp_path):
    g = Gate(tmp_path, {"events": [{"entities": [MOTION], "debounce_s": 0.05}]})
    g.change(MOTION, "off", "on")
    g.stack.agents.delete("room")
    g.gate.reload()
    await asyncio.sleep(0.1)
    assert g.wakes == []


@pytest.mark.parametrize("expr", ["* * * *", "61 * * * *", "every day", "* * * * * *"])
def test_bad_cron_expressions_are_rejected_at_save_time(expr):
    from app.core.agents import ConfigError, parse_agent

    with pytest.raises(ConfigError):
        parse_agent({**AGENT, "wake_rules": {"cron": [{"expr": expr}]}})
