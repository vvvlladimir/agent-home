"""The council (D40): ownership, the actors the actuator knows, house modes and the orchestrator's
wakes. Ownership and the mode helper are part of the safety layer: nothing actuates what its actor
does not own."""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.config import RuntimeSettings
from app.core.council import LEARNER, MODE_ENTITY, ORCHESTRATOR, in_window, parse_window
from tests.helpers import ROOM, FakeLearner, StubHA, go_live, make_stack


def house():
    ha = StubHA(ROOM)
    ha.registry.areas["hall"] = {"area_id": "hall", "name": "Hall"}
    ha.add("light.hall_ceiling", "off", area="hall")
    ha.add("light.no_area", "off")
    ha.add("switch.zigbee_permit_join", "off")
    ha.add("person.owner", "home")
    ha.add("sun.sun", "above_horizon")
    ha.add(MODE_ENTITY, "home")
    ha.states[MODE_ENTITY]["attributes"] = {"options": ["home", "away", "night", "guests", "work"]}
    return ha


def stack(tmp_path, learner=None, **settings):
    return make_stack(tmp_path, ha=house(), settings=RuntimeSettings(**settings), learner=learner)


# --- ownership -----------------------------------------------------------------------------------
def test_every_entity_has_exactly_one_owner_and_pairing_switches_none(tmp_path):
    s = stack(tmp_path)
    owner = s.council.ownership.owner_of
    for entity_id in ("light.room_ceiling", "light.hall_ceiling", "light.no_area", "person.owner", MODE_ENTITY):
        assert owner(entity_id) == ORCHESTRATOR
    assert owner("switch.zigbee_permit_join") is None, "nothing of ours may open the Zigbee network"
    assert owner(None) is None and owner("not an id") is None
    summary = s.council.ownership.summary()
    assert summary["counts"] == {ORCHESTRATOR: len(s.ha.states) - 1, "nobody": 1}


async def test_a_pairing_switch_is_never_actuated_even_in_scope(tmp_path):
    s = stack(tmp_path, learner=FakeLearner(["switch.zigbee_permit_join"]), manager_enabled=True)
    go_live(s, ORCHESTRATOR, LEARNER)
    assert "switch.zigbee_permit_join" not in s.manager.owned()
    action = await s.actuator.request(LEARNER, "switch", "turn_on", "switch.zigbee_permit_join")
    assert action["verdict"] == "denied" and "owned by nobody" in action["reason"]
    assert s.ha.calls == []


def test_the_actors_are_the_orchestrator_and_a_configured_learner(tmp_path):
    s = stack(tmp_path)
    orchestrator = s.council.actor(ORCHESTRATOR)
    assert orchestrator["mode"] == "shadow" and orchestrator["entities"] == [MODE_ENTITY], "manager off"
    assert s.council.actor(LEARNER) is None, "no learner configured"
    for unknown in ("room", "hall", "owner", "", None, 7):
        assert s.council.actor(unknown) is None
    s.council.learner = FakeLearner(["light.room_lamp"])
    assert s.council.actor(LEARNER)["entities"] == ["light.room_lamp"]
    s.council.settings = RuntimeSettings(manager_enabled=True, orchestrator_mode="live", learner_mode="live")
    s.manager.settings = s.council.settings
    assert MODE_ENTITY in s.council.actor(ORCHESTRATOR)["entities"]
    assert "light.hall_ceiling" in s.council.actor(ORCHESTRATOR)["entities"]
    assert {s.council.actor(a)["mode"] for a in (ORCHESTRATOR, LEARNER)} == {"live"}


async def test_the_learner_cannot_switch_the_house_mode(tmp_path):
    s = stack(tmp_path, learner=FakeLearner([MODE_ENTITY]))  # even if the Jetson named it
    go_live(s, LEARNER)
    action = await s.actuator.request(LEARNER, "input_select", "select_option", MODE_ENTITY, {"option": "away"})
    assert action["verdict"] == "denied"
    assert s.ha.calls == []


# --- house mode ---------------------------------------------------------------------------------
def test_night_window():
    window = parse_window("23:00-07:00")
    assert in_window(window, datetime(2026, 11, 13, 23, 30)) and in_window(
        window, datetime(2026, 11, 13, 6, 59)
    )
    assert not in_window(window, datetime(2026, 11, 13, 7, 0)) and not in_window(
        window, datetime(2026, 11, 13, 12)
    )
    assert parse_window("bad") is None and parse_window("10:00-10:00") is None


async def test_auto_mode_goes_through_the_actuator_in_shadow_by_default(tmp_path):
    s = stack(tmp_path, auto_house_mode=True)
    s.safety.set_mode("live")
    s.ha.states["person.owner"]["state"] = "not_home"
    action = await s.council.evaluate_mode("test", datetime(2026, 11, 13, 12))
    assert action["verdict"] == "shadow" and action["agent_id"] == ORCHESTRATOR and action["tier"] == "T1"
    assert s.ha.calls == []
    assert await s.council.evaluate_mode("again", datetime(2026, 11, 13, 12)) is None, "asked once only"


async def test_in_shadow_the_way_back_is_recorded_too(tmp_path):
    """The helper stays `home` in shadow; away after leaving and home after coming back are both
    recorded, each once (stories 2026-11-15: only away ever showed)."""
    s = stack(tmp_path, auto_house_mode=True)
    s.ha.states["person.owner"]["state"] = "not_home"
    assert (await s.council.evaluate_mode("left", datetime(2026, 11, 13, 12)))["data"] == {"option": "away"}
    s.ha.states["person.owner"]["state"] = "home"
    back = await s.council.evaluate_mode("back", datetime(2026, 11, 13, 18))
    assert back["verdict"] == "shadow" and back["data"] == {"option": "home"}
    assert await s.council.evaluate_mode("again", datetime(2026, 11, 13, 18)) is None


async def test_auto_mode_live_switches_the_helper_and_never_leaves_owner_modes(tmp_path):
    s = stack(tmp_path, auto_house_mode=True, orchestrator_mode="live")
    s.safety.set_mode("live")
    action = await s.council.evaluate_mode("night", datetime(2026, 11, 13, 23, 30))
    assert action["verdict"] == "executed"
    assert s.ha.calls == [{"domain": "input_select", "service": "select_option", "entity_id": MODE_ENTITY,
                           "data": {"option": "night"}}]  # fmt: skip
    s.ha.states[MODE_ENTITY]["state"] = "guests"
    s.council._requested = None
    s.ha.states["person.owner"]["state"] = "not_home"
    assert await s.council.evaluate_mode("left", datetime(2026, 11, 13, 12)) is None
    s.safety.set_kill(True, "test")
    s.ha.states[MODE_ENTITY]["state"] = "home"
    action = await s.council.evaluate_mode("left", datetime(2026, 11, 13, 12))
    assert action["verdict"] == "denied" and "kill switch" in action["reason"]


async def test_auto_mode_is_off_by_default(tmp_path):
    s = stack(tmp_path)
    s.ha.states["person.owner"]["state"] = "not_home"
    assert await s.council.evaluate_mode("test") is None


async def test_the_orchestrators_wake_rules_are_listed_with_their_last_firing(tmp_path):
    s = stack(tmp_path, auto_house_mode=True)
    wakes = {w["id"]: w for w in s.council.status()["orchestrator_wakes"]}
    assert list(wakes) == ["person", "daily_review", "manager_sweep"]
    assert all(w["last"] is None and w["rule"] for w in wakes.values()) and wakes["person"]["active"]
    s.council.on_state("person.owner", {"state": "home"}, {"state": "not_home"})
    await asyncio.sleep(0)
    fired = {w["id"]: w["last"] for w in s.council.status()["orchestrator_wakes"]}
    assert fired["person"] and fired["daily_review"] is None
    assert s.store.get("orchestrator_wakes") == {k: v for k, v in fired.items() if v}
    off = stack(tmp_path)
    assert {w["id"]: w["active"] for w in off.council.status()["orchestrator_wakes"]}["person"] is False
    assert s.council.status()["learner_mode"] == "shadow"
