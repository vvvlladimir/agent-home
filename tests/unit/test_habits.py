"""D31 habit dossiers: which devices are relevant, what the owner does by hand, how regular it is,
and the text the orchestrator gets. Pure functions over Change lists and a registry."""

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


def build(changes, **kw):
    kw = {"days": 10, "min_days": 3, "policy": DEFAULT_POLICY, **kw}
    return habits.build(sorted(changes, key=lambda c: c.ts), house(), **kw)


def test_workday_and_weekend_times_are_one_habit_with_two_bands():
    [dossier], funnel = build(kitchen())
    assert dossier["entity_id"] == "light.kitchen" and dossier["days"] == 10
    on = next(s for s in dossier["states"] if s["state"] == "on")
    bands = {t["days"]: t for t in on["time"]}
    assert set(bands) == {"Mon–Fri", "Sat–Sun"}
    assert bands["Mon–Fri"]["at"].startswith("06:4") and bands["Mon–Fri"]["on_time"] == 8
    assert bands["Sat–Sun"]["at"].startswith("08:") and on["support"] == 1.0
    text = habits.request(dossier)
    assert "Mon–Fri median 06:4" in text and "Sat–Sun median 08:" in text and "ONE automation" in text
    assert "samples: Mon 11-02 06:40" in text
    assert funnel["relevant"] == 1 and funnel["dossiers"] == 1


def test_a_lamp_after_the_rooms_sensor_is_a_signal_with_its_precision():
    changes = []
    for day in range(6):  # evenings at varying times: the time says little, the sensor says it all
        t = at(day, 18 + day % 4, 7 * day % 50)
        changes += [sensor(t, "binary_sensor.desk_zone", "on"), hand(t + 40, "light.desk", "on"),
                    sensor(t + 3600, "binary_sensor.desk_zone", "off")]  # fmt: skip
    for day in range(2):  # the sensor also turns on without the lamp
        changes += [sensor(at(day, 10, 0), "binary_sensor.desk_zone", "on"),
                    sensor(at(day, 10, 30), "binary_sensor.desk_zone", "off")]  # fmt: skip
    changes.append(sensor(at(0, 0, 1), "binary_sensor.hall_motion", "on"))  # another room: no signal
    [dossier], _ = build(changes)
    on = next(s for s in dossier["states"] if s["state"] == "on")
    [after] = on["after"]
    assert (after["signal"], after["state"], after["before"], after["of"]) == (
        "binary_sensor.desk_zone",
        "on",
        6,
        6,
    )
    assert after["median_delay_s"] == 40 and after["precision"] == 0.75
    assert "followed 75% of the time" in habits.request(dossier)
    assert "after binary_sensor.desk_zone → on (6/6)" in habits.summary(dossier)


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
    dossiers, funnel = build(changes, skip=frozenset({"light.kitchen"}))
    assert dossiers == [] and funnel["relevant"] == 0
    left = funnel["left_out"]
    assert left["one of our automations already acts on it"] == 1
    assert left["hidden or disabled"] == 1 and left["a device setting or diagnostic"] == 1
    assert left["a pairing switch"] == 1 and left["T3: never automated"] == 1
    assert left["changes too often to be a person (it switches itself)"] == 1
    assert funnel["near"] == [{"entity_id": "light.porch", "days": 2, "needed": 3}]
    assert "light.desk" not in str(funnel), "automated changes are never habits"


def test_an_irregular_device_is_relevant_but_no_dossier():
    times = [(0, 3, 10), (1, 14, 0), (2, 22, 40), (3, 9, 15), (4, 18, 5), (5, 1, 0)]
    dossiers, funnel = build([hand(at(d, h, m), "light.random", "on") for d, h, m in times])
    assert dossiers == [] and funnel["relevant"] == 1 and funnel["irregular"] == 1
