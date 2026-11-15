"""Which devices are worth a habit prediction (relevance; the predictions: test_predict.py)."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.core import habits
from app.core.insights import Change
from app.core.policy import DEFAULT_POLICY
from app.ha.registry import Registry

# 2026-11-02 is a Monday
BASE = datetime(2026, 11, 2, 0, 0).astimezone()
AREAS = {
    "light.kitchen": "kitchen", "light.desk": "office", "binary_sensor.desk_zone": "office",
    "binary_sensor.hall_motion": "hall", "light.porch": "porch", "switch.plug": "office",
    "light.hidden": "office", "switch.zigbee2mqtt_bridge_permit_join": None, "cover.garage_door": "hall",
    "light.status_led": "office", "light.random": "office", "switch.laptop_internet_access": None,
}  # fmt: skip


def house() -> SimpleNamespace:
    entities = [{"entity_id": e, "area_id": a} for e, a in AREAS.items()]
    for e in entities:
        if e["entity_id"] == "light.hidden":
            e["hidden_by"] = "user"
        if e["entity_id"] == "switch.laptop_internet_access":
            e["entity_category"] = "config"
        if e["entity_id"] == "binary_sensor.desk_zone":
            e["original_device_class"] = "occupancy"
    areas = [{"area_id": a} for a in {a for a in AREAS.values() if a}]
    states = {e: {"state": "off", "attributes": {}} for e in AREAS}
    states["sun.sun"] = {"state": "below_horizon", "attributes": {}}
    states["person.owner"] = {"state": "home", "attributes": {}}
    return SimpleNamespace(states=states, registry=Registry.build(areas, [], entities))


def at(day: int, hour: int, minute: int, second: int = 0) -> float:
    return (BASE + timedelta(days=day, hours=hour, minutes=minute, seconds=second)).timestamp()


def hand(ts: float, entity_id: str, state: str) -> Change:
    return Change(ts, entity_id, state, "user")


def sensor(ts: float, entity_id: str, state: str) -> Change:
    return Change(ts, entity_id, state, "physical")


def kitchen(days: int = 10) -> list[Change]:
    """On at ~06:45 on workdays and ~08:30 at weekends, off ~50 min later (the sim's routine)."""
    out = []
    for day in range(days):
        wake = (6, 40 + day % 3 * 4) if (BASE + timedelta(days=day)).weekday() < 5 else (8, 25 + day % 2 * 9)
        out += [
            hand(at(day, *wake), "light.kitchen", "on"),
            hand(at(day, wake[0], wake[1] + 50), "light.kitchen", "off"),
        ]
    return out


def test_relevance_leaves_out_noise_settings_t3_and_self_switching_and_says_why():
    changes = kitchen()
    for day in range(10):
        for entity in ("light.hidden", "switch.zigbee2mqtt_bridge_permit_join", "switch.laptop_internet_access",
                       "cover.garage_door"):  # fmt: skip
            changes.append(hand(at(day, 9, 0), entity, "on"))
        for minute in range(0, 60, 2):  # a plug that reports itself 30 times an hour, "physical"
            changes.append(sensor(at(day, 12, minute), "switch.plug", "on" if minute % 4 else "off"))
    changes += [hand(at(0, 20, 0), "light.porch", "on"), hand(at(1, 20, 0), "light.porch", "on")]
    changes += [Change(at(d, 7, 0), "light.desk", "on", "automation", "automation.x") for d in range(10)]
    candidates, funnel = habits.relevant(
        sorted(changes, key=lambda c: c.ts), house(), min_days=3, policy=DEFAULT_POLICY, skip={"light.kitchen"}
    )
    assert candidates == [] and funnel["relevant"] == 0
    left = funnel["left_out"]
    assert left["one of our automations already acts on it"] == 1
    assert left["hidden or disabled"] == 1 and left["a device setting or diagnostic"] == 1
    assert left["a pairing switch"] == 1 and left["T3: never automated"] == 1
    assert left["changes too often to be a person (it switches itself)"] == 1
    assert funnel["near"] == [{"entity_id": "light.porch", "days": 2, "needed": 3}]
    assert "light.desk" not in str(funnel), "automated changes are never habits"
