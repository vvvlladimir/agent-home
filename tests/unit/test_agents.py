"""Agent config: the pydantic model mirrors the JSON Schema; the store; the entity scope."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from app.core.agents import (
    AgentConfig,
    AgentStore,
    ConfigError,
    EventRule,
    Limits,
    PolicyModel,
    WakeRules,
    parse_agent,
    parse_policy,
    resolve_scope,
)
from app.core.policy import DEFAULT_POLICY, resolve_tier
from tests.helpers import AGENT, StubHA

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "contracts/schemas/agent_config.schema.json").read_text())
FULL = {
    **AGENT,
    "entities": ["light.room_ceiling", "binary_sensor.room_motion"],
    "tier_overrides": {"light.*": "T1"},
    "wake_rules": {
        "events": [{"entities": ["binary_sensor.room_motion"], "to": ["on"], "from": ["off"], "above": 1}],
        "cron": [{"expr": "0 7 * * *", "reason": "morning"}],
    },
    "limits": {"max_steps": 3},
}


def _defaults(model) -> dict:
    return {
        (field.alias or name): field.get_default(call_default_factory=True)
        for name, field in model.model_fields.items()
        if not field.is_required()
    }


@pytest.mark.parametrize(
    ("model", "schema"),
    [
        (AgentConfig, SCHEMA),
        (WakeRules, SCHEMA["properties"]["wake_rules"]),
        (Limits, SCHEMA["properties"]["limits"]),
        (EventRule, SCHEMA["properties"]["wake_rules"]["properties"]["events"]["items"]),
    ],
)
def test_model_and_schema_have_the_same_fields_and_defaults(model, schema):
    properties = schema["properties"]
    assert {f.alias or n for n, f in model.model_fields.items()} == set(properties)
    for name, default in _defaults(model).items():
        if "default" in properties[name]:
            dumped = default.model_dump() if hasattr(default, "model_dump") else default
            expected = properties[name]["default"]
            assert dumped == expected or (expected == {} and dumped), name


def test_policy_model_matches_the_policy_schema():
    schema = json.loads((ROOT / "contracts/schemas/policy.schema.json").read_text())
    assert set(PolicyModel.model_fields) == set(schema["properties"]) == set(DEFAULT_POLICY)
    assert parse_policy(DEFAULT_POLICY) == DEFAULT_POLICY
    for bad in ({"domain_tiers": {"light": "T9"}}, {"t3_whitelist": ["lock.*"]}, {"surprise": 1},
                {"rate_limits": {"global_per_minute": 0}}, {"approval_ttl_s": 5}, []):  # fmt: skip
        with pytest.raises(ConfigError):
            parse_policy(bad)


def test_what_the_model_accepts_the_schema_accepts_and_the_dump_round_trips():
    agent = parse_agent(FULL)
    jsonschema.validate(agent.dump(), SCHEMA)
    assert parse_agent(agent.dump()) == agent
    assert agent.dump()["wake_rules"]["events"][0]["from"] == ["off"]
    assert agent.mode == "shadow", "agents are born in shadow mode"


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "Room"},
        {"id": "../etc"},
        {"mode": "yolo"},
        {"entities": [f"light.l{i}" for i in range(26)]},
        {"entities": ["not an entity"]},
        {"tier_overrides": {"light.x": "T9"}},
        {"limits": {"max_steps": 99}},
        {"surprise": True},
        {"wake_rules": {"events": [{"entities": []}]}},
        {"entities": ["light.room_ceiling"], "wake_rules": {"events": [{"entities": ["sensor.elsewhere"]}]}},
    ],
)
def test_the_model_rejects_what_the_schema_rejects(bad):
    with pytest.raises(ConfigError):
        parse_agent({**AGENT, **bad})
    with pytest.raises(ConfigError):
        parse_agent("not a dict")


def test_store_round_trip_and_bad_files_are_skipped(tmp_path):
    store = AgentStore(tmp_path)
    store.save(parse_agent(FULL))
    (tmp_path / "agents" / "broken.yaml").write_text("id: broken\nmode: yolo\n")
    (tmp_path / "agents" / "renamed.yaml").write_text("id: other\nname: X\nroom: r\n")
    again = AgentStore(tmp_path)
    assert [a.id for a in again.list()] == ["room"] and again.get("room") == parse_agent(FULL)
    assert again.delete("room") and not again.delete("room") and not again.delete("../../x")
    assert AgentStore(tmp_path).list() == []


def tier_of(entity_id: str) -> str:
    return resolve_tier(entity_id)


def test_scope_is_the_room_actuators_first_capped():
    ha = StubHA()
    ha.add("light.other_room", "off", area="elsewhere")
    ha.add("sensor.room_uptime", "5", area="room")
    ha.registry.entity_meta["sensor.room_uptime"] = {"entity_category": "diagnostic"}
    ha.add("light.room_hidden", "off", area="room")
    ha.registry.entity_meta["light.room_hidden"] = {"hidden_by": "user"}
    included, dropped = resolve_scope(parse_agent({**AGENT, "max_entities": 6}), ha, tier_of)
    assert included == [
        "light.room_ceiling", "light.room_lamp", "climate.room", "scene.room_movie", "switch.room_heater",
        "binary_sensor.room_motion",
    ]  # fmt: skip
    assert dropped == ["lock.room_door", "sensor.room_illuminance"], "a quarter is kept for room sensors"


def test_an_explicit_entity_list_is_the_scope():
    ha = StubHA()
    agent = parse_agent({**AGENT, "entities": ["light.room_lamp", "light.gone"]})
    assert resolve_scope(agent, ha, tier_of) == (["light.room_lamp"], [])


def test_device_noise_is_filtered_and_button_stays():
    ha = StubHA()
    for entity_id in (
        "update.room_lamp_firmware", "device_tracker.room_phone", "sensor.room_lamp_linkquality",
        "sensor.room_plug_rssi", "sensor.room_remote_battery", "button.room_tv_restart",
    ):  # fmt: skip
        ha.add(entity_id, "1", area="room")
    ha.add("sensor.room_remote_level", "80", area="room")
    ha.states["sensor.room_remote_level"]["attributes"]["device_class"] = "battery"
    shown, findable = resolve_scope(parse_agent({**AGENT, "max_entities": 25}), ha, tier_of)
    scope = shown + findable
    assert "button.room_tv_restart" in scope, "buttons are real actions, not noise"
    for noise in ("update.room_lamp_firmware", "device_tracker.room_phone", "sensor.room_lamp_linkquality",
                  "sensor.room_plug_rssi", "sensor.room_remote_battery", "sensor.room_remote_level"):  # fmt: skip
        assert noise not in scope


def test_room_sensors_rank_before_other_sensors_and_the_rest_stays_findable():
    ha = StubHA()
    ha.add("sensor.room_power", "12", area="room")
    ha.states["binary_sensor.room_motion"]["attributes"]["device_class"] = "motion"
    shown, findable = resolve_scope(parse_agent({**AGENT, "max_entities": 7}), ha, tier_of)
    assert shown[-1] == "binary_sensor.room_motion"
    assert "sensor.room_power" in findable and "sensor.room_illuminance" in findable


def test_pinned_entities_go_first_and_the_room_stays_findable():
    ha = StubHA()
    pinned = ["sensor.room_illuminance", "lock.room_door"]
    only = parse_agent({**AGENT, "entities": pinned, "max_entities": 3})
    assert resolve_scope(only, ha, tier_of) == (pinned, []), "only = exactly the ticked ones"
    agent = parse_agent({**AGENT, "entities": pinned, "entities_mode": "pinned", "max_entities": 3})
    shown, findable = resolve_scope(agent, ha, tier_of)
    assert shown == pinned + ["light.room_ceiling"]
    assert "light.room_lamp" in findable and not set(pinned) & set(findable)
    # a wake rule may watch a room entity that is not ticked
    rule = {"events": [{"entities": ["binary_sensor.room_motion"]}]}
    parse_agent({**AGENT, "entities": pinned, "entities_mode": "pinned", "wake_rules": rule})
    with pytest.raises(ConfigError):
        parse_agent({**AGENT, "entities": pinned, "wake_rules": rule})


def test_watched_and_used_entities_lead_unavailable_and_indicators_trail():
    ha = StubHA()
    ha.add("light.room_box_rgb_light", "on", area="room")
    ha.states["light.room_lamp"]["state"] = "unavailable"
    agent = parse_agent({**AGENT, "max_entities": 25, "wake_rules": {"events": [{"entities": ["lock.room_door"]}]}})
    shown, _ = resolve_scope(agent, ha, tier_of, usage={"switch.room_heater": 3, "climate.room": 1})
    assert shown[:3] == ["lock.room_door", "switch.room_heater", "climate.room"]
    assert set(shown[-2:]) == {"light.room_box_rgb_light", "light.room_lamp"}


def test_the_owners_livingroom_prompt_shows_working_devices_and_room_sensors():
    """Regression: run r_1a1d4212c23abec23. 20 prompt slots went to unavailable devices and sensor
    boxes' RGB LEDs; the agent could not see its room and looped on list_entities."""
    case = yaml.safe_load((Path(__file__).parents[1] / "regressions" / "livingroom_owner_run.yaml").read_text())
    ha = StubHA()
    for row in case["room"]["entities"]:
        ha.add(row["entity_id"], row["state"], area="living")
    for sensor in ("binary_sensor.esphome_radar_livingroom_occupancy", "sensor.esphome_air_quality_temperature",
                   "sensor.livingroom_humidity", "binary_sensor.livingroom_window"):  # fmt: skip
        ha.add(sensor, "on", area="living")
    shown, findable = resolve_scope(parse_agent({**AGENT, "room": "living", "max_entities": 20}), ha, tier_of)
    sensors = [e for e in shown if e.startswith(("sensor.", "binary_sensor."))]
    assert len(sensors) >= 4, "the room sensors are in the prompt"
    working = [e for e in shown if ha.states[e]["state"] != "unavailable" and not e.endswith("_rgb_light")]
    assert shown[: len(working)] == working, "unavailable devices and indicator LEDs come last"
    assert "light.garland" in shown + findable, "unavailable is ranked down, never removed"


def test_settings_an_agent_cannot_change_trail_the_room_sensors():
    """Regression: run r_1a1d43953a55cf263. Six T3 `number.*_off_delay` / `*_countdown` settings took
    prompt slots ahead of the room's sensors; the model then summarised settings, not the room."""
    path = Path(__file__).parents[1] / "regressions" / "livingroom_owner_run_2.yaml"
    case = yaml.safe_load(path.read_text())
    ha = StubHA()
    for row in case["room"]["entities"]:
        ha.add(row["entity_id"], row["state"], area="living")
    for sensor in ("sensor.livingroom_temperature", "sensor.livingroom_humidity", "binary_sensor.livingroom_window",
                   "sensor.livingroom_illuminance", "sensor.livingroom_co2", "sensor.livingroom_pm25"):  # fmt: skip
        ha.add(sensor, "1", area="living")
    shown, findable = resolve_scope(parse_agent({**AGENT, "room": "living", "max_entities": 20}), ha, tier_of)
    numbers = [i for i, e in enumerate(shown) if e.startswith("number.")]
    sensors = [i for i, e in enumerate(shown) if e.startswith(("sensor.", "binary_sensor."))]
    room = ha.registry.entities_in("living")
    assert all(e in shown for e in room if e.startswith(("sensor.", "binary_sensor."))), "every sensor fits"
    assert not numbers or min(numbers) > max(sensors), "T3 settings come after the sensors"
    assert "number.socket_3d_printer_countdown" in shown + findable, "ranked down, never removed"
