"""Suggested wake rules (core/wake_suggest.py): from a room's devices, deterministic, exact ids."""

from __future__ import annotations

from app.core.agents import AgentConfig, parse_agent, resolve_scope
from app.core.wake_suggest import kind_of, suggest
from tests.helpers import StubHA


def house() -> StubHA:
    ha = StubHA()
    for entity_id, state, cls in [
        ("binary_sensor.kitchen_leak_sensor_water_leak", "off", "moisture"),
        ("binary_sensor.kitchen_leak_sensor_battery_low", "off", "battery"),
        ("binary_sensor.kitchen_window", "off", "window"),
        ("binary_sensor.kitchen_motion_occupancy", "off", "occupancy"),
        ("sensor.kitchen_temperature", "21.0", "temperature"),
        ("sensor.kitchen_plug_device_temperature", "40", "temperature"),
        ("switch.kitchen_fridge", "on", "outlet"),
        ("switch.kitchen_dishwasher", "off", "outlet"),
        ("lock.front_door", "locked", None),
        ("lock.kitchen_dishwasher_child_lock", "unlocked", None),
        ("cover.garage_door", "closed", "garage"),
        ("cover.kitchen_blinds", "open", "blind"),
        ("light.kitchen_ceiling", "off", None),
    ]:
        ha.add(entity_id, state, area="kitchen")
        if cls:
            ha.states[entity_id]["attributes"]["device_class"] = cls
    ha.registry.entity_meta["binary_sensor.kitchen_leak_sensor_battery_low"] = {"entity_category": "diagnostic"}
    ha.registry.entity_meta["lock.kitchen_dishwasher_child_lock"] = {"entity_category": "config"}
    return ha


def test_one_rule_per_kind_with_exact_ids_and_a_reason():
    rules = {r["reason"].split(" ")[0]: r for r in suggest(house(), "kitchen")}
    by_entity = {e: r for r in suggest(house(), "kitchen") for e in r["entities"]}
    leak = by_entity["binary_sensor.kitchen_leak_sensor_water_leak"]
    assert leak["priority"] == "alarm" and leak["debounce_s"] == 0 and leak["to"] == ["on"]
    assert by_entity["lock.front_door"]["to"] == ["unlocked", "open", "jammed"]
    assert "never act" in by_entity["cover.garage_door"]["reason"]
    assert by_entity["switch.kitchen_fridge"]["to"] == ["off", "unavailable"]
    assert by_entity["sensor.kitchen_temperature"]["below"] == 16.0
    assert by_entity["binary_sensor.kitchen_leak_sensor_battery_low"]["cooldown_s"] == 86400
    assert by_entity["binary_sensor.kitchen_window"]["to"] == ["on"]
    assert by_entity["binary_sensor.kitchen_motion_occupancy"]["cooldown_s"] == 300
    left_out = {"switch.kitchen_dishwasher", "lock.kitchen_dishwasher_child_lock", "cover.kitchen_blinds",
                "light.kitchen_ceiling", "sensor.kitchen_plug_device_temperature"}  # fmt: skip
    assert not left_out & set(by_entity)
    assert all(len(r["reason"]) <= 120 for r in rules.values())
    assert suggest(house(), "attic") == []


def test_every_suggestion_is_a_valid_agent_config():
    rules = suggest(house(), "kitchen")
    agent = parse_agent({"id": "kitchen", "name": "Kitchen", "room": "kitchen", "wake_rules": {"events": rules}})
    assert isinstance(agent, AgentConfig) and len(agent.wake_rules.events) == len(rules)


def test_a_watched_diagnostic_sensor_enters_the_scope_but_never_a_setting():
    """The low-battery sensor is device noise, out of scope; a wake rule on it would never fire.
    Naming it in a rule brings it in; a config entity named in a rule stays out (no new actuator)."""
    ha = house()
    plain = parse_agent({"id": "kitchen", "name": "K", "room": "kitchen"})
    shown, rest = resolve_scope(plain, ha, lambda e: "T0")
    assert "binary_sensor.kitchen_leak_sensor_battery_low" not in shown + rest
    watching = parse_agent({"id": "kitchen", "name": "K", "room": "kitchen", "wake_rules": {"events": [
        {"entities": ["binary_sensor.kitchen_leak_sensor_battery_low", "lock.kitchen_dishwasher_child_lock"]}]}})  # fmt: skip
    shown, rest = resolve_scope(watching, ha, lambda e: "T0")
    assert "binary_sensor.kitchen_leak_sensor_battery_low" in shown + rest
    assert "lock.kitchen_dishwasher_child_lock" not in shown + rest


def test_kinds_by_id_when_there_is_no_device_class():
    expected = {"binary_sensor.bath_water_leak": "alarm", "binary_sensor.hall_door": "opening",
                "binary_sensor.hall_motion": "presence", "binary_sensor.x_battery_low": "battery",
                "cover.driveway_gate": "garage", "switch.garage_freezer": "power",
                "binary_sensor.hall_illuminance_ok": None}  # fmt: skip
    ha = StubHA()
    for e in expected:
        ha.add(e, "off", area="hall")
    assert {e: kind_of(ha, e) for e in expected} == expected


def test_no_cold_rule_outdoors():
    ha = StubHA()
    ha.add("sensor.balcony_temperature", "3.0", area="balcony")
    ha.add("switch.balcony_fridge", "on", area="balcony")
    kinds = [r["reason"] for r in suggest(ha, "balcony")]
    assert kinds == ["A fridge or freezer lost power."]
