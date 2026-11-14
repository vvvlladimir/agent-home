"""Agent runtime: context, limits, escalation, traces, queueing, replay, copy-as-test-case."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from app.config import RuntimeSettings
from app.core.llm import LLMDropped, LLMError, LLMTimeout
from app.core.runtime import WakeRefused
from tests.helpers import AGENT, ScriptedLLM, call, go_live, make_stack, run, settle, steps, turn_on

ROOT = Path(__file__).resolve().parents[2]
OPENAPI = yaml.safe_load((ROOT / "contracts" / "openapi.yaml").read_text())
CEILING = "light.room_ceiling"


def stack_with(tmp_path, *script, **kw):
    return make_stack(tmp_path, llm=ScriptedLLM(list(script)), **kw)


def validate_run(payload: dict) -> None:
    schema = {"$ref": "#/components/schemas/Run", "components": OPENAPI["components"]}
    jsonschema.validate(payload, schema, cls=jsonschema.Draft202012Validator)


# --- context --------------------------------------------------------------------------------


def test_prompt_prefix_is_stable_and_the_changing_part_is_in_the_user_message(tmp_path):
    stack = stack_with(tmp_path)
    agent = stack.agents.get("room")
    first = stack.rt.build_context(agent, "motion")
    stack.ha.states[CEILING]["state"] = "on"
    stack.store.execute("INSERT INTO memory VALUES ('room', 'bedtime', '23:00', 0)")
    second = stack.rt.build_context(agent, "something else entirely")
    assert first["system"] == second["system"], (
        "the system prompt must not depend on states, notes or the wake"
    )
    assert f"- {CEILING} ({CEILING}) [T0]" in first["system"] and "[T3]" in first["system"]
    assert f"{CEILING}: on" in second["user"] and "- bedtime: 23:00" in second["user"]
    assert second["user"].endswith("Wake reason: something else entirely")
    assert "sensor.room_illuminance: 4 lx" in first["user"]
    assert "climate.room: heat temperature=21" in first["user"]


def test_context_is_trimmed_to_the_token_budget_sensors_first(tmp_path):
    stack = stack_with(tmp_path)
    for index in range(17):
        stack.ha.add(f"sensor.room_filler_{index:02d}", "1", area="room")
    agent = stack.agents.get("room").model_copy(update={"max_entities": 25})
    roomy = stack.rt.build_context(agent, "x")
    assert roomy["entities_included"] == 25 and roomy["entities_dropped"] == []
    tight = agent.model_copy(
        update={"limits": agent.limits.model_copy(update={"context_budget_tokens": 1900})}
    )
    trimmed = stack.rt.build_context(tight, "x")
    assert trimmed["tokens_estimate"] <= 1900 < roomy["tokens_estimate"]
    assert trimmed["entities_dropped"] and all(e.startswith("sensor.") for e in trimmed["entities_dropped"])
    assert CEILING in trimmed["system"]


def test_custom_prompt_template(tmp_path):
    custom = {
        **AGENT,
        "system_prompt": "You are {{agent_name}} in {{room_name}}.\n{{entity_table}}\n{{tiers}}",
    }
    stack = stack_with(tmp_path, agents=[custom])
    system = stack.rt.build_context(stack.agents.get("room"), "x")["system"]
    assert system.startswith("You are Room in Room.\n- ") and "T3 = you can never actuate it" in system
    bare = {**AGENT, "system_prompt": "Be brief."}
    stack = stack_with(tmp_path / "b", agents=[bare])
    assert CEILING in stack.rt.build_context(stack.agents.get("room"), "x")["system"], (
        "entities are always listed"
    )


# --- the loop -------------------------------------------------------------------------------


async def test_a_shadow_run_is_traced_and_matches_the_contract(tmp_path):
    stack = stack_with(tmp_path, turn_on(CEILING), {"text": "Turned the ceiling light on."})
    result = await run(stack, reason="motion in the dark")
    validate_run(result)
    assert result["status"] == "ok" and result["shadow"] is True and result["steps"] == 2
    assert result["summary"] == "Turned the ceiling light on." and result["tokens_in"] == 200
    assert [s["type"] for s in result["trace"]] == ["context", "llm", "tool", "policy", "llm", "final"]
    assert steps(result, "policy")[0]["verdict"] == "shadow" and stack.ha.calls == []
    assert steps(result, "tool")[0]["result"]["status"] == "recorded"
    sent = stack.llm.requests[1]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool"]
    assert sent[0]["content"] == stack.llm.requests[0]["messages"][0]["content"]
    assert json.loads(sent[3]["content"])["ok"] is True
    assert {t["function"]["name"] for t in stack.llm.requests[0]["tools"]} >= {"call_service", "remember"}
    assert stack.feed.recent(kind="run")[0]["run_id"] == result["id"]


async def test_live_run_actuates_through_the_policy(tmp_path):
    stack = stack_with(
        tmp_path,
        {"tool_calls": [
            {"name": "call_service", "arguments": {"service": "light.turn_on", "entity_id": [CEILING, "light.room_lamp"]}},
            {"name": "call_service", "arguments": {"domain": "lock", "service": "unlock", "entity_id": "lock.room_door"}},
            {"name": "call_service", "arguments": {"domain": "switch", "service": "turn_on", "entity_id": "switch.room_heater"}},
        ]},
    )  # fmt: skip
    go_live(stack)
    result = await run(stack)
    assert result["shadow"] is False
    assert [c["entity_id"] for c in stack.ha.calls] == [CEILING, "light.room_lamp"], (
        "only the T0 calls went out"
    )
    assert [p["verdict"] for p in steps(result, "policy")] == ["executed", "executed", "denied", "pending"]
    tools = steps(result, "tool")
    assert tools[0]["result"]["ok"] and len(tools[0]["result"]["results"]) == 2
    assert "denied by policy" in tools[1]["result"]["error"]
    assert tools[2]["result"]["status"] == "pending_approval"


async def test_tool_argument_shapes_the_model_gets_wrong(tmp_path):
    stack = stack_with(
        tmp_path,
        call("call_service", domain="light", service="turn_on", entity_id=CEILING, brightness_pct=30),
        call("call_service", domain="light", service="turn_on", entity_id=CEILING, data="bright"),
        call("call_service", domain="light", service="turn_on", entity_id="light.kitchen"),
        call("get_state", entity_id="sensor.somewhere_else"),
    )
    go_live(stack)
    result = await run(stack)
    errors = [t["result"].get("error", "") for t in steps(result, "tool")]
    assert "unexpected argument 'brightness_pct'" in errors[0] and "must be an object" in errors[1]
    assert "denied by policy" in errors[2] and "unknown entity" in errors[3]
    assert stack.ha.calls == []


async def test_escalates_after_unusable_tool_calls_instead_of_guessing(tmp_path):
    broken = {"name": "call_service", "raw_arguments": '{"domain": "light", "service": '}
    stack = stack_with(tmp_path, broken, broken, turn_on(CEILING))
    go_live(stack)
    result = await run(stack)
    assert result["status"] == "escalated" and "unusable tool calls" in result["error"]
    assert stack.ha.calls == [] and len(stack.llm.requests) == 2
    assert stack.rt.inbox()[0]["severity"] == "warning" and "Run stopped" in stack.rt.inbox()[0]["summary"]
    told = json.loads(stack.llm.requests[1]["messages"][-1]["content"])
    assert told["ok"] is False, "the model is told its call was unusable before the run gives up"


async def test_a_repaired_call_is_used_and_traced(tmp_path):
    fenced = '```json\n{"domain": "light", "service": "turn_on", "entity_id": "light.room_ceiling",}\n```'
    stack = stack_with(tmp_path, {"name": "call_service", "raw_arguments": fenced})
    result = await run(stack)
    assert result["status"] == "ok" and steps(result, "repair")[0]["repaired"]["entity_id"] == CEILING
    assert steps(result, "policy")[0]["verdict"] == "shadow"


async def test_content_embedded_and_unknown_tool_calls(tmp_path):
    stack = stack_with(
        tmp_path,
        {"content_tool_call": {"name": "call_service", "arguments": {"domain": "light", "service": "turn_on", "entity_id": CEILING}}},
        call("open_pod_bay_doors"),
    )  # fmt: skip
    result = await run(stack)
    tools = steps(result, "tool")
    assert tools[0]["ok"] and "unknown tool" in tools[1]["result"]["error"] and result["status"] == "ok"


async def test_step_limit_escalates(tmp_path):
    script = [
        call("get_state", entity_id=e) for e in ("light.room_ceiling", "light.room_lamp", "climate.room")
    ]
    limited = {**AGENT, "limits": {"max_steps": 3}}
    stack = stack_with(tmp_path, *script, agents=[limited])
    result = await run(stack)
    assert result["status"] == "escalated" and "step limit of 3" in result["error"] and result["steps"] == 3
    assert len(stack.llm.requests) == 3


async def test_a_repeated_action_is_refused_then_escalates(tmp_path):
    stack = stack_with(tmp_path, *[turn_on(CEILING)] * 5)
    go_live(stack)
    result = await run(stack)
    assert result["status"] == "escalated" and "repeated the same" in result["error"]
    assert len(stack.ha.calls) == 1, "the second identical call is refused, not sent"
    second = steps(result, "tool")[1]["result"]
    assert second["ok"] is False and "already made this exact call" in second["error"]


async def test_reading_again_is_allowed_with_a_nudge(tmp_path):
    read = call("get_state", entity_id=CEILING)
    stack = stack_with(tmp_path, read, read, read, {"text": "All fine."})
    result = await run(stack)
    assert result["status"] == "ok"
    tools = [t["result"] for t in steps(result, "tool")]
    assert all(t["ok"] for t in tools) and "note" not in tools[0] and "already read" in tools[2]["note"]


async def test_time_limit_counts_model_and_tool_time(tmp_path):
    slow = {**call("get_state", entity_id=CEILING), "ms": 3000}
    stack = stack_with(tmp_path, slow, slow, slow, agents=[{**AGENT, "limits": {"timeout_s": 5}}])
    result = await run(stack)
    assert result["status"] == "timeout" and len(stack.llm.requests) == 2
    assert stack.llm.requests[1]["timeout_s"] == pytest.approx(2, abs=0.2), "the model gets what is left"


@pytest.mark.parametrize(
    ("error", "status"),
    [(LLMTimeout("too slow"), "timeout"), (LLMDropped("stale"), "dropped"), (LLMError("HTTP 500"), "error")],
)
async def test_model_failures_end_the_run_cleanly(tmp_path, error, status):
    stack = stack_with(tmp_path, {"raise": error})
    result = await run(stack)
    assert result["status"] == status and result["ended"] and stack.ha.calls == []
    validate_run(result)


async def test_a_truncated_empty_answer_escalates(tmp_path):
    stack = stack_with(tmp_path, {"text": "", "finish": "length"})
    assert (await run(stack))["status"] == "escalated"


async def test_an_escalating_report_marks_the_run_and_reaches_the_inbox(tmp_path):
    stack = stack_with(
        tmp_path, call("report", severity="escalate", summary="Smoke detector is on."), {"text": "Reported."}
    )
    result = await run(stack)
    assert result["status"] == "escalated"
    inbox = stack.rt.inbox()
    assert inbox[0]["severity"] == "alarm" and inbox[0]["summary"] == "Smoke detector is on."
    bad = stack_with(tmp_path / "b", call("report", severity="panic", summary="x"))
    assert "severity" in steps(await run(bad), "tool")[0]["result"]["error"]


# --- the other tools --------------------------------------------------------------------------


async def test_read_tools(tmp_path):
    stack = stack_with(
        tmp_path,
        call("get_state", entity_id="sensor.room_illuminance"),
        call("list_entities", domain="light"),
        call("get_history", entity_id=CEILING, hours=2),
    )
    tools = [t["result"] for t in steps(await run(stack), "tool")]
    assert tools[0] == {"ok": True, "state": "4", "attributes": {}, "unit": "lx"}
    assert [e["entity_id"] for e in tools[1]["entities"]] == [CEILING, "light.room_lamp"]
    assert tools[1]["entities"][0]["tier"] == "T0"
    assert tools[2]["changes"] == [
        {"at": "2026-11-13T20:00", "state": "off"},
        {"at": "2026-11-13T21:00", "state": "on"},
    ]


async def test_memory_is_capped_and_shows_up_in_the_next_wake(tmp_path):
    stack = stack_with(
        tmp_path,
        call("remember", key="bedtime", value="The owner goes to bed around 23:00."),
        call("remember", key="big", value="x" * 300),
        call("recall"),
        call("remember", key="bedtime", value=""),
        agents=[{**AGENT, "memory_cap_chars": 100}],
    )
    tools = [t["result"] for t in steps(await run(stack), "tool")]
    assert tools[0]["status"] == "saved" and "memory is full" in tools[1]["error"]
    assert tools[2]["notes"] == {"bedtime": "The owner goes to bed around 23:00."}
    assert tools[3]["status"] == "deleted" and stack.rt.memory("room") == {}


async def test_schedule_wake_and_ask_agent(tmp_path):
    other = {"id": "hall", "name": "Hall", "room": "hall"}
    stack = stack_with(
        tmp_path,
        call("schedule_wake", in_minutes=10, reason="check the heater"),
        call("schedule_wake", in_minutes=0, reason="now"),
        call("ask_agent", agent_id="hall", question="Is anyone in the hall?"),
        call("ask_agent", agent_id="hall", question="Again?"),
        call("ask_agent", agent_id="nobody", question="?"),
        {"text": "Done."},
        call("ask_agent", agent_id="room", question="ping-pong"),  # the hall agent's run
        agents=[{**AGENT, "limits": {"max_steps": 8}}, other],
    )
    first = stack.rt.wake("room", source="manual", reason="test")
    await settle(stack)
    tools = [t["result"] for t in steps(stack.rt.get_run(first), "tool")]
    assert tools[0]["ok"] and not tools[1]["ok"]
    assert stack.store.query("SELECT reason FROM scheduled_wakes") == [{"reason": "check the heater"}]
    assert (
        tools[2]["status"] == "forwarded"
        and tools[2]["to"] == "hall"
        and "one question" in tools[3]["error"]
        and "one question" in tools[4]["error"]
    )
    # M5: the question goes through the orchestrator, which gives the hall agent a task
    asked = stack.rt.list_runs(agent="hall")[0]
    assert asked["wake_source"] == "task"
    assert "Task from the orchestrator (asked by room): Is anyone in the hall?" in asked["wake_reason"]
    answer = steps(stack.rt.get_run(asked["id"]), "tool")[0]["result"]
    assert not answer["ok"], "a task cannot ask further: no chains"
    assert len(stack.rt.list_runs()) == 2
    board = {(r["agent_id"], r["kind"]) for r in stack.council.board.list()}
    assert {("room", "escalation"), ("orchestrator", "task"), ("hall", "report")} <= board


# --- queueing ---------------------------------------------------------------------------------


async def test_one_run_per_agent_and_newer_event_wakes_replace_waiting_ones(tmp_path):
    stack = stack_with(tmp_path, {"text": "first", "sleep": 0.1})
    ids = [stack.rt.wake("room", source="event", reason=f"event {i}") for i in range(4)]
    manual = stack.rt.wake("room", source="manual", reason="owner")
    await settle(stack)
    statuses = [stack.rt.get_run(i)["status"] for i in ids]
    assert statuses == ["ok", "dropped", "dropped", "ok"], "only the newest waiting event wake survives"
    assert stack.rt.get_run(manual)["status"] == "ok", "a manual wake is never coalesced away"
    assert "superseded" in stack.rt.get_run(ids[1])["error"]
    assert len(stack.llm.requests) == 3


async def test_waiting_wakes_go_stale(tmp_path):
    stack = stack_with(
        tmp_path, {"text": "slow", "sleep": 0.15}, settings=RuntimeSettings(llm_stale_after_s=1)
    )
    stack.rt.wake("room", source="event", reason="first")
    late = stack.rt.wake("room", source="event", reason="second")
    stack.rt._waiting["room"][0]["started"] -= 5
    await settle(stack)
    assert stack.rt.get_run(late)["status"] == "dropped" and "stale" in stack.rt.get_run(late)["error"]


async def test_wakes_are_refused_under_the_kill_switch_and_for_disabled_agents(tmp_path):
    stack = stack_with(tmp_path)
    stack.safety.set_kill(True, "test")
    with pytest.raises(WakeRefused) as refused:
        stack.rt.wake("room", source="event", reason="x")
    assert refused.value.code == "kill_switch" and stack.llm.requests == []
    stack.safety.set_kill(False)
    stack.agents.save(stack.agents.get("room").model_copy(update={"enabled": False}))
    with pytest.raises(WakeRefused):
        stack.rt.wake("room", source="manual", reason="x")
    with pytest.raises(WakeRefused):
        stack.rt.wake("ghost", source="manual", reason="x")


async def test_kill_switch_engaged_while_a_wake_waits(tmp_path):
    stack = stack_with(tmp_path, {"text": "first", "sleep": 0.05}, turn_on(CEILING))
    go_live(stack)
    stack.rt.wake("room", source="manual", reason="a")
    second = stack.rt.wake("room", source="manual", reason="b")
    stack.safety.set_kill(True, "now")
    await settle(stack)
    assert stack.rt.get_run(second)["status"] == "dropped" and stack.ha.calls == []


async def test_runs_left_over_from_a_crash_are_closed_at_startup(tmp_path):
    stack = stack_with(tmp_path)
    stack.store.execute(
        "INSERT INTO runs (id, agent_id, status, wake_source, started) VALUES ('r_old', 'room', 'running', 'event', 1)"
    )
    type(stack.rt)(
        agents=stack.agents,
        ha=stack.ha,
        llm=stack.llm,
        store=stack.store,
        feed=stack.feed,
        safety=stack.safety,
    )
    assert stack.rt.get_run("r_old")["status"] == "error"


# --- replay and copy-as-test-case -------------------------------------------------------------


async def test_replay_is_always_shadow_and_leaves_no_side_effects(tmp_path):
    stack = stack_with(tmp_path, turn_on(CEILING), {"text": "On."})
    go_live(stack)
    original = await run(stack, reason="motion in the dark")
    assert len(stack.ha.calls) == 1
    stack.ha.states[CEILING]["state"] = "off"
    stack.llm.steps = [
        turn_on(CEILING),
        call("report", severity="escalate", summary="replayed"),
        call("remember", key="k", value="v"),
        call("schedule_wake", in_minutes=5, reason="r"),
    ]
    replay_id = stack.rt.replay(original["id"])
    await settle(stack)
    replay = stack.rt.get_run(replay_id)
    validate_run(replay)
    assert (
        replay["replay_of"] == original["id"]
        and replay["wake_source"] == "replay"
        and replay["shadow"] is True
    )
    assert steps(replay, "context")[0]["user"] == steps(original, "context")[0]["user"], "the recorded prompt"
    assert steps(replay, "policy")[0]["verdict"] == "shadow" and len(stack.ha.calls) == 1
    assert stack.rt.inbox() == [] and stack.rt.memory("room") == {}
    assert stack.store.query("SELECT * FROM scheduled_wakes") == []
    with pytest.raises(WakeRefused):
        stack.rt.replay("r_missing")


async def test_copy_as_test_case_is_a_valid_eval_file(tmp_path):
    stack = stack_with(tmp_path, turn_on(CEILING), call("report", severity="info", summary="Light on."))
    original = await run(stack, reason="motion in the dark")
    text = stack.rt.testcase(original["id"])
    doc = yaml.safe_load(text)
    schema = json.loads((ROOT / "contracts/schemas/toolcall_eval.schema.json").read_text())
    schema["properties"]["scenarios"]["minItems"] = 1
    jsonschema.validate(doc, schema)
    scenario = doc["scenarios"][0]
    assert scenario["wake"] == "motion in the dark" and scenario["category"] == "valid_call"
    assert scenario["expect"]["calls"] == [
        {"tool": "call_service", "args": {"service": "turn_on", "entity_id": CEILING}},
        {"tool": "report", "args": {"severity": "info"}},
    ]
    entity = next(e for e in doc["room"]["entities"] if e["entity_id"] == "lock.room_door")
    assert entity == {"entity_id": "lock.room_door", "state": "locked", "tier": "T3"}
    assert text.startswith("# Copied from run")
    await asyncio.sleep(0)


async def test_entities_outside_the_prompt_are_findable_and_usable(tmp_path):
    stack = stack_with(
        tmp_path,
        call("list_entities", search="ILLUMIN"),
        call("get_state", entity_id="sensor.room_illuminance"),
        agents=[{**AGENT, "max_entities": 2}],
    )
    agent = stack.agents.get("room")
    context = stack.rt.build_context(agent, "test")
    assert "sensor.room_illuminance" not in context["system"]
    assert "more in your room: find them with list_entities" in context["system"]
    assert "sensor.room_illuminance" in stack.rt.scope(agent)
    tools = [t["result"] for t in steps(await run(stack), "tool")]
    assert [e["entity_id"] for e in tools[0]["entities"]] == ["sensor.room_illuminance"]
    assert tools[1]["ok"] is True


async def test_used_entities_are_remembered_but_not_by_replays(tmp_path):
    stack = stack_with(tmp_path, call("get_state", entity_id="sensor.room_illuminance"), {"text": "ok"})
    await run(stack)
    assert stack.rt.usage("room") == {"sensor.room_illuminance": 1}


async def test_a_scheduled_wake_cannot_schedule_or_ask_again(tmp_path):
    stack = stack_with(
        tmp_path,
        call("schedule_wake", in_minutes=15, reason="check again"),
        call("ask_agent", agent_id="other", question="anything?"),
        {"text": "Nothing to do."},
    )
    result = await run(stack, source="schedule")
    tools = [t["result"] for t in steps(result, "tool")]
    assert result["status"] == "ok" and not tools[0]["ok"] and not tools[1]["ok"]
    assert "Finish with one short sentence" in tools[0]["error"]


async def test_one_schedule_wake_per_run(tmp_path):
    stack = stack_with(
        tmp_path,
        call("schedule_wake", in_minutes=15, reason="door still open"),
        call("schedule_wake", in_minutes=30, reason="and again"),
        {"text": "Scheduled."},
    )
    tools = [t["result"] for t in steps(await run(stack), "tool")]
    assert tools[0]["ok"] is True and tools[1]["ok"] is False


async def test_an_agent_proposes_an_automation_once_and_replays_do_not(tmp_path):
    asked = []
    stack = stack_with(
        tmp_path,
        call("propose_automation", description="lamp on at motion after dark"),
        call("propose_automation", description="and again the same idea"),
        {"text": "Proposed."},
    )
    stack.rt.orchestrator = type("Orch", (), {"request_draft": lambda self, *a: asked.append(a) or True})()
    result = await run(stack)
    outputs = [s["result"] for s in steps(result, "tool")]
    assert outputs[0]["ok"] and outputs[0]["status"] == "queued"
    assert not outputs[1]["ok"] and "one proposal" in outputs[1]["error"]
    assert asked == [("room", result["id"], "lamp on at motion after dark", "room")]
    stack.llm.steps = [call("propose_automation", description="lamp on at motion after dark"), {"text": "ok"}]
    stack.rt.replay(result["id"])
    await settle(stack)
    assert len(asked) == 1, "a replay never drafts"


def test_a_second_alarm_about_the_same_entity_updates_the_first(tmp_path):
    """QA-001: one leak, one inbox item that stays current; other alarms and info stay separate."""
    from tests.helpers import make_stack

    s = make_stack(tmp_path)
    leak = "binary_sensor.kitchen_leak_sensor_water_leak"
    s.rt.inbox_add("kitchen", "r1", "alarm", f"Water leak: {leak} is on")
    s.rt.inbox_add("orchestrator", "r2", "alarm", f"{leak} is still on after the kitchen's call")
    s.rt.inbox_add("hall", "r3", "alarm", "Smoke: binary_sensor.hall_smoke is on")
    s.rt.inbox_add("kitchen", "r4", "info", f"{leak} is off again")
    rows = s.store.query("SELECT agent_id, severity, summary FROM inbox ORDER BY id")
    assert [(r["agent_id"], r["severity"]) for r in rows] == [
        ("orchestrator", "alarm"),
        ("hall", "alarm"),
        ("kitchen", "info"),
    ]
    assert rows[0]["summary"].endswith("after the kitchen's call")
