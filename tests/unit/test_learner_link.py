"""The add-on's link to the Jetson learner (D38, core/learner.py): causes from the HA context, numeric
sensors once a minute, the registry document, and — the safety part — what a live suggestion may do:
only through the actuator, only where the orchestrator owns the entity, shadow unless learner_mode is
live, never with the kill switch, once per stretch."""

from __future__ import annotations

from types import SimpleNamespace

import jsonschema
import pytest

from app.core.learner import ANSWER_ENTITY, LEARNER, LearnerLink
from tests.helpers import StubHA, make_stack
from tests.unit.test_learner import schema

ENTITIES = [
    {"entity_id": "light.room_lamp", "state": "off"},  # area "room": the room agent owns it
    {"entity_id": "light.study_lamp", "state": "off"},  # area "study": no agent, the orchestrator owns it
    {"entity_id": "binary_sensor.study_motion", "state": "off"},
    {"entity_id": "sensor.study_lux", "state": "10"},
    {"entity_id": "lock.front_door", "state": "locked"},
    {"entity_id": "input_select.agent_house_mode", "state": "guests"},
]


def settings(**kw) -> SimpleNamespace:
    base = {"learner_mode": "shadow", "habit_days": 14, "habit_min_repeats": 3, "orchestrator_mode": "shadow"}
    return SimpleNamespace(**{**base, **kw})


def make(tmp_path, **kw):
    ha = StubHA(ENTITIES)
    ha.registry.areas["study"] = {"area_id": "study", "name": "Study"}
    for e in ("light.study_lamp", "binary_sensor.study_motion", "sensor.study_lux"):
        ha.registry.entity_area[e] = "study"
    ha.own_user_id = "our-user"
    ha.connected = True
    runtime = settings(**kw)
    stack = make_stack(tmp_path, ha=ha, settings=runtime)
    insights = SimpleNamespace(ours=lambda: {}, programs=None, _save=lambda *a, **k: None)
    link = LearnerLink(
        SimpleNamespace(jetson_learner_url="http://jetson:9200", jetson_token="t"),
        runtime, stack.store, stack.feed, ha, insights, stack.safety,
    )  # fmt: skip
    link.actuator, stack.council.learner = stack.actuator, link
    link._programs = {"nodered-user": "Node-RED"}
    stack.store.set("learner_model", {"trained_at": 1e12, "live": [
        {"entity_id": "input_select.agent_house_mode", "state": "away", "fires": 9, "right": 8, "right_days": 5},
        {"entity_id": "light.study_lamp", "state": "on", "fires": 9, "right": 8, "right_days": 5},
        {"entity_id": "light.room_lamp", "state": "on", "fires": 9, "right": 8, "right_days": 5},
        {"entity_id": "lock.front_door", "state": "unlocked", "fires": 9, "right": 8, "right_days": 5},
    ]})  # fmt: skip
    return stack, link


def state(value: str, **context) -> dict:
    return {"state": value, "last_changed": "2026-11-16T18:00:00+00:00",
            "context": {"id": context.pop("id", "c1"), "parent_id": None, "user_id": None, **context}}  # fmt: skip


def suggestion(entity_id: str, state_: str = "on", **kw) -> dict:
    return {"entity_id": entity_id, "state": state_, "p": 0.95, "mode": "act", "earned": True, **kw}


# --- causes -------------------------------------------------------------------------------------------
def test_causes_are_read_from_the_context_like_the_logbook(tmp_path):
    _, link = make(tmp_path)
    link.on_event({"event_type": "automation_triggered", "data": {"entity_id": "automation.hall"},
                   "time_fired": "2026-11-16T18:00:00+00:00", "context": {"id": "auto-ctx"}})  # fmt: skip
    cases = [
        (state("on", id="auto-ctx"), ("automation", "automation.hall")),
        (state("on", user_id="our-user"), ("agent", None)),
        (state("on", user_id="nodered-user"), ("automation", "user:Node-RED")),
        (state("on", user_id="owner"), ("user", None)),
        (state("on", parent_id="p"), ("other", None)),
        (state("on"), ("physical", None)),
    ]
    for new, expected in cases:
        assert link.cause(new) == expected
    link.on_state("light.study_lamp", state("off"), state("on", user_id="owner"))
    link.on_state("light.study_lamp", state("on"), state("on"))  # attribute-only: nothing
    link.on_event({"event_type": "mobile_app_notification_action", "data": {"action": "AGENT_HOUSE_X_NO"},
                   "time_fired": "2026-11-16T18:01:00+00:00"})  # fmt: skip
    link.on_event({"event_type": "mobile_app_notification_action", "data": {"action": "SOMEONE_ELSES"}})
    kinds = [(e["kind"], e["entity_id"], e.get("state"), e["cause"]) for e in link.buffer]
    assert kinds == [
        ("trigger", "automation.hall", None, "automation"),
        ("state", "light.study_lamp", "on", "user"),
        ("answer", ANSWER_ENTITY, "AGENT_HOUSE_X_NO", "user"),
    ]
    jsonschema.validate({"schema_version": 1, "events": list(link.buffer)}, schema("house_events.schema.json"))


def test_numeric_sensors_go_once_a_minute_with_their_last_value(tmp_path):
    _, link = make(tmp_path)
    for i, value in enumerate(("10", "11", "12")):
        new = {**state(value), "last_changed": f"2026-11-16T18:00:{10 + i * 10:02d}+00:00"}
        link.on_state("sensor.study_lux", None, new)
    assert not link.buffer
    link.flush_numeric(1794852030.0)  # still 18:00
    assert not link.buffer
    link.flush_numeric(1794852061.0)  # 18:01
    assert [(e["entity_id"], e["state"]) for e in link.buffer] == [("sensor.study_lux", "12")]


def test_the_buffer_is_bounded_and_a_full_buffer_asks_for_a_backfill(tmp_path):
    _, link = make(tmp_path)
    link.buffer = type(link.buffer)(maxlen=3)
    link._need_backfill = False
    for i in range(5):
        link.on_state("light.study_lamp", None, {**state("on" if i % 2 else "off"), "last_changed": float(i)})
    assert len(link.buffer) == 3 and link.dropped == 2 and link._need_backfill


def test_the_registry_document_follows_its_contract(tmp_path):
    stack, link = make(tmp_path)
    doc = link.registry_doc()
    jsonschema.validate(doc, schema("house_registry.schema.json"))
    assert {e["entity_id"]: e["area_id"] for e in doc["entities"]}["light.study_lamp"] == "study"
    assert doc["policy"] == stack.safety.policy


# --- live suggestions: the safety part -----------------------------------------------------------------
async def test_a_suggestion_goes_through_the_actuator_in_shadow_by_default(tmp_path):
    stack, link = make(tmp_path)
    stack.safety.set_mode("live")
    action = await link.consider(suggestion("light.study_lamp"))
    assert action["agent_id"] == LEARNER and action["verdict"] == "shadow"
    assert stack.ha.calls == []


async def test_live_only_where_the_orchestrator_owns_the_entity(tmp_path):
    stack, link = make(tmp_path, learner_mode="live")
    stack.safety.set_mode("live")
    lamp = await link.consider(suggestion("light.study_lamp"))
    assert lamp["verdict"] == "executed" and stack.ha.calls[-1]["entity_id"] == "light.study_lamp"
    room = await link.consider(suggestion("light.room_lamp"))  # the room agent owns it
    assert room["verdict"] == "denied"
    door = await link.consider(suggestion("lock.front_door", "unlocked"))  # never: T3 / not switchable
    assert door is None or door["verdict"] == "denied"
    assert [c["entity_id"] for c in stack.ha.calls] == ["light.study_lamp"]


async def test_kill_switch_global_shadow_and_repeats(tmp_path):
    stack, link = make(tmp_path, learner_mode="live")
    stack.safety.set_mode("shadow")  # the global mode wins
    assert (await link.consider(suggestion("light.study_lamp")))["verdict"] == "shadow"
    assert await link.consider(suggestion("light.study_lamp")) is None  # once per stretch / hour
    link._acted.clear()
    stack.safety.set_mode("live")
    stack.safety.set_kill(True, "test", source="test")
    assert (await link.consider(suggestion("light.study_lamp")))["verdict"] == "denied"
    assert stack.ha.calls == []


@pytest.mark.parametrize("bad", [{"mode": "ask"}, {"earned": False}, {"state": "off"}])
async def test_only_earned_acts_for_a_device_not_already_there(tmp_path, bad):
    stack, link = make(tmp_path, learner_mode="live")
    stack.safety.set_mode("live")
    stack.ha.states["light.study_lamp"]["state"] = "off"
    assert await link.consider({**suggestion("light.study_lamp"), **bad}) is None
    assert stack.ha.calls == []


def test_a_room_agent_cannot_be_called_learner(tmp_path):
    from app.core.agents import ConfigError, parse_agent

    with pytest.raises(ConfigError, match="reserved"):
        parse_agent({"id": "learner", "name": "x", "room": "room"})



async def test_never_a_helper_and_nothing_from_a_stale_model(tmp_path, monkeypatch):
    """The Jetson names the targets: the add-on only accepts devices, and only from a fresh model."""
    stack, link = make(tmp_path, learner_mode="live")
    stack.safety.set_mode("live")
    stack.ha.registry.entity_area["input_select.agent_house_mode"] = None  # the orchestrator owns it
    assert "input_select.agent_house_mode" not in link.targets()
    assert await link.consider(suggestion("input_select.agent_house_mode", "away")) is None
    stack.store.set("learner_model", {**stack.store.get("learner_model"), "trained_at": 0})  # stale
    assert link.targets() == []
    assert await link.consider(suggestion("light.study_lamp")) is None
    assert stack.ha.calls == []
