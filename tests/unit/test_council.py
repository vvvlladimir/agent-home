"""M5 agent council: ownership, the shared board, house modes, tasks down and escalation up.
Ownership and the mode helper are part of the safety layer: no agent actuates what it does not own."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.config import RuntimeSettings
from app.core.agents import ConfigError, parse_agent
from app.core.council import MODE_ENTITY, ORCHESTRATOR, in_window, parse_window
from tests.helpers import AGENT, ROOM, StubHA, call, go_live, make_stack, run, settle, steps, turn_on

HALL = {"id": "hall", "name": "Hall", "room": "hall"}


def house(**settings):
    ha = StubHA(ROOM)
    ha.registry.areas["hall"] = {"area_id": "hall", "name": "Hall"}
    ha.add("light.hall_ceiling", "off", area="hall")
    ha.add("person.owner", "home")
    ha.add("sun.sun", "above_horizon")
    ha.add(MODE_ENTITY, "home")
    ha.states[MODE_ENTITY]["attributes"] = {"options": ["home", "away", "night", "guests", "work"]}
    return ha


def stack(tmp_path, *script, agents=None, **settings):
    s = make_stack(tmp_path, ha=house(), agents=agents or [AGENT, HALL], settings=RuntimeSettings(**settings))
    s.llm.steps = list(script)
    return s


# --- ownership -----------------------------------------------------------------------------------
def test_every_entity_has_exactly_one_owner(tmp_path):
    s = stack(tmp_path, agents=[AGENT, HALL, {"id": "zz_room", "name": "Second", "room": "room",
                                             "entities": ["light.room_lamp"], "entities_mode": "pinned"}])  # fmt: skip
    owner = s.council.ownership.owner_of
    assert owner("light.room_ceiling") == "room"  # two agents in one room: the lowest id
    assert owner("light.room_lamp") == "zz_room"  # unless the other one lists the entity
    assert owner("light.hall_ceiling") == "hall"
    assert owner("person.owner") == owner(MODE_ENTITY) == owner("sun.sun") == ORCHESTRATOR
    s.ha.registry.areas["attic"] = {"area_id": "attic"}
    s.ha.add("light.attic", "off", area="attic")
    assert owner("light.attic") == ORCHESTRATOR  # an area without an agent
    summary = s.council.ownership.summary()
    assert summary["shared_rooms"] == {"room": ["room", "zz_room"]}
    assert sum(summary["counts"].values()) == len(s.ha.states)


async def test_an_agent_cannot_actuate_a_neighbours_entity_even_when_pinned(tmp_path):
    pinned = {**AGENT, "entities": ["light.hall_ceiling", "light.room_lamp"], "entities_mode": "pinned"}
    s = stack(tmp_path, turn_on("light.hall_ceiling"), turn_on("light.room_lamp"), {"text": "ok"},
              agents=[pinned, HALL])  # fmt: skip
    go_live(s)
    result = await run(s)
    decisions = steps(result, "policy")
    assert decisions[0]["verdict"] == "denied" and "owned by hall" in decisions[0]["reason"]
    assert decisions[1]["verdict"] == "executed"
    assert [c["entity_id"] for c in s.ha.calls] == ["light.room_lamp"]
    context = s.rt.build_context(s.agents.get("room"), "x")
    assert "light.hall_ceiling (light.hall_ceiling) [read-only, owned by hall]" in context["system"]


async def test_no_room_agent_can_switch_the_house_mode(tmp_path):
    s = stack(tmp_path)
    go_live(s)
    s.agents.save(
        s.agents.get("room").model_copy(update={"entities": [MODE_ENTITY], "entities_mode": "pinned"})
    )
    action = await s.actuator.request(
        "room", "input_select", "select_option", MODE_ENTITY, {"option": "away"}
    )
    assert action["verdict"] == "denied" and "owned by orchestrator" in action["reason"]
    assert s.ha.calls == []


def test_orchestrator_id_is_reserved_for_room_agents():
    with pytest.raises(ConfigError, match="reserved"):
        parse_agent({"id": "orchestrator", "name": "x", "room": "room"})


# --- board -------------------------------------------------------------------------------------
async def test_facts_reach_the_neighbours_prompt_and_expire(tmp_path):
    s = stack(tmp_path, call("post_fact", text="someone went to bed at 23:10", minutes=30),
              call("post_fact", text="b", minutes=30), call("post_fact", text="third fact", minutes=30),
              call("post_fact", text="four", minutes=30), {"text": "ok"})  # fmt: skip
    result = await run(s)
    tools = [t["result"] for t in steps(result, "tool")]
    assert tools[0]["ok"] and not tools[1]["ok"] and tools[2]["ok"] and "two facts" in tools[3]["error"]
    hall = s.rt.build_context(s.agents.get("hall"), "x")["user"]
    assert "House mode: home" in hall and "room (0 min ago): someone went to bed at 23:10" in hall
    own = s.rt.build_context(s.agents.get("room"), "x")["user"]
    assert "someone went to bed" not in own, "an agent does not see its own facts"
    s.council.board.clock = lambda: 9e12  # far in the future: everything expired
    assert "someone went to bed" not in s.rt.build_context(s.agents.get("hall"), "x")["user"]


async def test_a_replay_posts_nothing(tmp_path):
    s = stack(tmp_path, call("post_fact", text="fact from the original run"), {"text": "ok"})
    original = await run(s)
    s.llm.steps = [call("post_fact", text="fact from the replay"), {"text": "ok"}]
    s.rt.replay(original["id"])
    await settle(s)
    texts = [r["text"] for r in s.council.board.list()]
    assert texts == ["fact from the original run"]


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


# --- tasks down, escalation up -------------------------------------------------------------------
async def test_a_mode_change_gives_every_room_a_task_and_the_answers_land_on_the_board(tmp_path):
    s = stack(tmp_path, {"text": "Room: all off."}, {"text": "Hall: all off."})
    s.council.on_state(MODE_ENTITY, {"state": "home"}, {"state": "away"})
    await settle(s)
    runs = s.rt.list_runs()
    assert {r["agent_id"] for r in runs} == {"room", "hall"} and {r["wake_source"] for r in runs} == {"task"}
    assert all("House mode is now away" in r["wake_reason"] for r in runs)
    reports = [r for r in s.council.board.list() if r["kind"] == "report"]
    assert {r["agent_id"] for r in reports} == {"room", "hall"}
    assert {r["text"] for r in reports} == {"Room: all off.", "Hall: all off."}
    s.council.on_state(MODE_ENTITY, {"state": "away"}, {"state": "home"})
    await settle(s)
    assert len(s.rt.list_runs()) == 2, "home hands out no tasks"


async def test_ask_names_an_entity_and_reaches_its_owner(tmp_path):
    s = stack(tmp_path, call("ask_agent", agent_id="orchestrator", question="Is light.hall_ceiling still on?"),
              {"text": "asked"}, {"text": "It is off."})  # fmt: skip
    await run(s)
    await settle(s)
    hall = s.rt.list_runs(agent="hall")
    assert len(hall) == 1 and hall[0]["wake_source"] == "task"
    s2 = stack(
        tmp_path, call("ask_agent", agent_id="orchestrator", question="Is the house ok?"), {"text": "ok"}
    )
    result = await run(s2)
    assert steps(result, "tool")[0]["result"]["status"] == "escalated"
    assert [r["kind"] for r in s2.council.board.list()] == ["escalation"]


async def test_escalate_reports_reach_the_orchestrator_board(tmp_path):
    s = stack(tmp_path, call("report", severity="escalate", summary="Smoke detected"), {"text": "reported"})
    await run(s)
    rows = s.council.board.list()
    assert rows[0]["kind"] == "escalation" and rows[0]["target"] == ORCHESTRATOR
    assert s.rt.inbox()[0]["severity"] == "alarm"


async def test_asks_are_capped_per_hour(tmp_path):
    s = stack(tmp_path)
    results = [s.council.ask("room", "hall", f"question {i}") for i in range(8)]
    assert sum(r["ok"] for r in results) <= 6 and "per hour" in results[-1]["error"]
    await settle(s)


# --- escalation reaches the orchestrator's triage ------------------------------------------------
ESCALATE = call("report", severity="escalate", summary="light.hall_ceiling is on and the hall is empty")


async def test_an_escalation_naming_another_owners_entity_is_forwarded_as_a_task(tmp_path):
    s = stack(tmp_path, ESCALATE, {"text": "reported"}, {"text": "Hall: switched it off."})
    result = await run(s)
    await settle(s)
    assert steps(result, "tool")[0]["result"] == {"ok": True, "status": "forwarded", "to": "hall"}
    [task] = s.rt.list_runs(agent="hall")
    assert task["wake_source"] == "task" and "(asked by room)" in task["wake_reason"]
    kinds = {(r["kind"], r["agent_id"], r["target"]) for r in s.council.board.list()}
    assert ("escalation", "room", "hall") in kinds and ("report", "hall", ORCHESTRATOR) in kinds
    assert s.rt.inbox()[-1]["severity"] == "alarm", "the owner still sees it"


@pytest.mark.parametrize("source", ["task", "chat"])
async def test_escalations_from_task_or_chat_runs_are_never_forwarded(tmp_path, source):
    s = stack(tmp_path, ESCALATE, {"text": "reported"})
    result = await run(s, source=source)
    await settle(s)
    assert steps(result, "tool")[0]["result"]["status"] == "on_board"
    assert s.rt.list_runs(agent="hall") == []
    escalations = [r for r in s.council.board.list() if r["kind"] == "escalation"]
    assert [r["target"] for r in escalations] == [ORCHESTRATOR]


async def test_escalations_without_another_owner_stay_for_the_review_and_are_bounded(tmp_path):
    s = stack(tmp_path)
    for text in ("Smoke detected", "person.owner left the stove on", "light.room_ceiling flickers"):
        assert s.council.escalate("room", text, source="manual")["status"] == "on_board"
    results = [s.council.escalate("room", f"light.hall_ceiling #{i}", source="manual") for i in range(8)]
    assert sum(r["status"] == "forwarded" for r in results) <= 6 and "per hour" in results[-1]["note"]
    assert s.council.ask("room", "hall", "one more?")["ok"] is False, "one hourly budget with ask_agent"
    s.agents.save(s.agents.get("hall").model_copy(update={"enabled": False}))
    assert s.council.escalate("zz", "light.hall_ceiling", source="manual")["status"] == "on_board"
    await settle(s)


async def test_the_orchestrators_wake_rules_are_listed_with_their_last_firing(tmp_path):
    s = stack(tmp_path, auto_house_mode=True)
    wakes = {w["id"]: w for w in s.council.status()["orchestrator_wakes"]}
    assert list(wakes) == ["house_mode", "person", "daily_review", "escalation"]
    assert all(w["last"] is None and w["rule"] for w in wakes.values()) and wakes["person"]["active"]
    s.council.on_state(MODE_ENTITY, {"state": "home"}, {"state": "night"})
    s.council.on_state("person.owner", {"state": "home"}, {"state": "not_home"})
    s.council.escalate("room", "Smoke detected", source="manual")
    await settle(s)
    fired = {w["id"]: w["last"] for w in s.council.status()["orchestrator_wakes"]}
    assert fired["house_mode"] and fired["person"] and fired["escalation"] and fired["daily_review"] is None
    assert s.store.get("orchestrator_wakes") == {k: v for k, v in fired.items() if v}
    off = stack(tmp_path)
    assert {w["id"]: w["active"] for w in off.council.status()["orchestrator_wakes"]}["person"] is False
