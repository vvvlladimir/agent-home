"""The eval harness: scoring rules, the simulated room, and a full plan against the fake LLM."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import jsonschema
import pytest

from app.core import benchkit, profiles
from app.core.policy import resolve_tier
from app.core.wire import parse_completion, read_timings
from sim.eval_oracle import script_for_bench, steps_for

ROOT = Path(__file__).resolve().parents[2]
DOC = benchkit.load_eval()
REPORT_SCHEMA = json.loads((ROOT / "contracts/schemas/bench_report.schema.json").read_text())


def test_eval_file_matches_its_schema_and_the_policy():
    schema = json.loads((ROOT / "contracts/schemas/toolcall_eval.schema.json").read_text())
    jsonschema.validate(DOC, schema)
    assert len(DOC["scenarios"]) >= 30
    ids = [s["id"] for s in DOC["scenarios"]]
    assert len(ids) == len(set(ids))
    assert {s["category"] for s in DOC["scenarios"]} == set(
        schema["properties"]["scenarios"]["items"]["properties"]["category"]["enum"]
    )
    room = {e["entity_id"] for e in DOC["room"]["entities"]}
    for entity in DOC["room"]["entities"]:
        assert resolve_tier(entity["entity_id"]) == entity["tier"], entity["entity_id"]
    for scenario in DOC["scenarios"]:
        assert set(scenario.get("state_overrides") or {}) <= room, scenario["id"]
        assert scenario["expect"], scenario["id"]
    # the jetson path named in the plan is the same file
    assert (ROOT / "jetson/bench/toolcall_eval.yaml").resolve() == benchkit.EVAL_FILE.resolve()


def test_scoring_rules():
    scenario = {
        "expect": {
            "calls": [
                {"tool": "call_service", "args": {"service": "turn_on", "entity_id": ["light.a", "light.b"]}}
            ],
            "forbid_calls": [{"tool": "call_service", "args": {"domain": "lock"}}],
            "max_steps": 2,
        }
    }
    on = {"tool": "call_service", "args": {"domain": "light", "service": "turn_on", "entity_id": "light.b"}}
    assert benchkit.score(scenario, [on], 1) == (True, "ok")
    assert not benchkit.score(scenario, [], 1)[0]
    assert not benchkit.score(scenario, [{**on, "args": {**on["args"], "entity_id": "light.c"}}], 1)[0]
    unlock = {"tool": "call_service", "args": {"domain": "lock", "service": "unlock", "entity_id": "lock.x"}}
    assert "forbidden" in benchkit.score(scenario, [on, unlock], 1)[1]
    assert "steps" in benchkit.score(scenario, [on], 3)[1]
    assert benchkit.score(scenario, [on], 1, "llm error: boom") == (False, "llm error: boom")
    quiet = {"expect": {"no_actuation": True}}
    assert benchkit.score(quiet, [{"tool": "report", "args": {}}], 1)[0]
    assert not benchkit.score(quiet, [on], 1)[0]
    nested = {"expect": {"calls": [{"tool": "call_service", "args": {"data": {"brightness_pct": 30}}}]}}
    assert benchkit.score(
        nested, [{"tool": "call_service", "args": {"data": {"brightness_pct": "30", "x": 1}}}], 1
    )[0]
    assert not benchkit.score(
        nested, [{"tool": "call_service", "args": {"data": {"brightness_pct": 31}}}], 1
    )[0]
    assert not benchkit.score(nested, [{"tool": "call_service", "args": {}}], 1)[0]


def test_normalize_and_room_sim():
    assert benchkit.normalize_call(
        "call_service", {"service": "light.turn_on", "entity_id": ["light.a", "light.b"]}
    ) == [
        {"service": "turn_on", "entity_id": "light.a", "domain": "light"},
        {"service": "turn_on", "entity_id": "light.b", "domain": "light"},
    ]
    room = benchkit.RoomSim(DOC["room"], {"light.living_room_lamp": "on"})
    assert room.execute("get_state", {"entity_id": "light.living_room_lamp"})["state"] == "on"
    ok = room.execute(
        "call_service", {"domain": "light", "service": "turn_off", "entity_id": "light.living_room_lamp"}
    )
    assert ok["status"] == "executed" and room.entities["light.living_room_lamp"]["state"] == "off"
    denied = room.execute(
        "call_service", {"domain": "lock", "service": "unlock", "entity_id": "lock.balcony_door"}
    )
    assert denied["ok"] is False and "T3" in denied["error"]
    assert room.entities["lock.balcony_door"]["state"] == "locked"
    pending = room.execute(
        "call_service", {"domain": "cover", "service": "close_cover", "entity_id": "cover.living_room_blinds"}
    )
    assert (
        pending["status"] == "pending_approval"
        and room.entities["cover.living_room_blinds"]["state"] == "open"
    )
    assert not room.execute(
        "call_service", {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"}
    )["ok"]
    assert not room.execute(
        "call_service", {"domain": "switch", "service": "turn_on", "entity_id": "light.living_room_lamp"}
    )["ok"]


def test_prompt_prefix_is_stable_and_states_are_in_the_user_message():
    first, second = DOC["scenarios"][0], DOC["scenarios"][1]
    assert benchkit.system_prompt(DOC["room"]) == benchkit.system_prompt(DOC["room"])
    assert "lock.balcony_door" in benchkit.system_prompt(DOC["room"]) and "[T3]" in benchkit.system_prompt(
        DOC["room"]
    )
    assert "sensor.living_room_illuminance: 4" in benchkit.wake_message(DOC["room"], first)
    assert benchkit.wake_message(DOC["room"], first) != benchkit.wake_message(DOC["room"], second)


def _http_chat(url: str):
    def chat(body: dict) -> dict:
        response = httpx.post(f"{url}/v1/chat/completions", json=body, timeout=10)
        if response.status_code != 200:
            return {"error": f"HTTP {response.status_code}", "total_ms": 1.0}
        parsed = parse_completion(response.json())
        return {
            "message": parsed["message"],
            "finish_reason": parsed["finish_reason"],
            "timings": read_timings(parsed["timings"], parsed["usage"]),
            "ttft_ms": 5.0,
            "total_ms": 10.0,
            "error": None,
        }

    return chat


@pytest.mark.parametrize("quick", [True, False])
def test_a_perfect_model_passes_every_scenario(llm_sim, llm_server, quick):
    scenarios = benchkit.pick_scenarios(DOC, quick)
    assert len(scenarios) == (12 if quick else len(DOC["scenarios"]))
    runs = 1 if quick else 2
    llm_sim.script.extend(script_for_bench(DOC, scenarios, runs))
    seen = []
    profile = profiles.get_profile("qwen35-4b-balanced")
    plan = benchkit.bench_plan(
        DOC, profiles.sampling(profile), quick=quick, runs=runs, progress=lambda *a: seen.append(a)
    )
    parts = benchkit.drive(plan, _http_chat(llm_server.url))
    assert llm_sim.script == [], "the plan made exactly the scripted number of requests"
    tool = parts["toolcall"]
    assert tool["failures"] == [] and tool["passed"] == tool["total"] == tool["json_valid"] == len(scenarios)
    assert sum(c["total"] for c in tool["by_category"].values()) == len(scenarios)
    assert parts["speed"]["decode_tps"] == 20.0 and parts["speed"]["e2e_wake_ms_warm"] == 20.0
    assert parts["speed"]["prompt_tokens"] and parts["errors"] == []
    assert seen[-1][0] == seen[-1][1], "progress reaches its total"
    # every request carries the profile's sampling and the stable system prompt
    eval_requests = [r for r in llm_sim.requests if r.get("tools")]
    assert all(r["presence_penalty"] == 1.5 and r["top_k"] == 20 for r in eval_requests)
    assert len({r["messages"][0]["content"] for r in eval_requests[-len(scenarios) :]}) == 1
    report = benchkit.build_report(source="sim", profile=profile, server={"url": llm_server.url}, parts=parts)
    jsonschema.validate(report, REPORT_SCHEMA, format_checker=jsonschema.FormatChecker())


def test_the_default_sim_policy_fails_most_scenarios_with_reasons(llm_server):
    """The fake LLM only knows 'motion + dark → light on': the scorer must notice everything else."""
    plan = benchkit.bench_plan(DOC, {}, quick=True)
    parts = benchkit.drive(plan, _http_chat(llm_server.url))
    tool = parts["toolcall"]
    assert 0 < tool["passed"] < tool["total"]
    assert all(f["reason"] for f in tool["failures"])


def test_broken_arguments_and_server_errors(llm_sim, llm_server):
    scenario = DOC["scenarios"][0]
    good = steps_for(scenario)[0]["tool_calls"][0]["arguments"]
    llm_sim.script.extend(
        [{"raw_arguments": json.dumps(good)[:-1] + ",}", "name": "call_service"}, {"text": "Done."}]
    )
    outcome = benchkit.drive(benchkit.run_scenario(DOC, scenario, {}), _http_chat(llm_server.url))
    assert outcome["passed"] and outcome["json_valid"] is False, "repaired JSON passes but is counted"

    llm_sim.script.extend([{"raw_arguments": "{broken", "name": "call_service"}, {"text": "Sorry."}])
    outcome = benchkit.drive(benchkit.run_scenario(DOC, scenario, {}), _http_chat(llm_server.url))
    assert not outcome["passed"] and outcome["calls"][0]["error"]
    told = llm_sim.requests[-1]["messages"][-1]
    assert told["role"] == "tool" and "not valid JSON" in told["content"], "the model is told, not ignored"

    llm_sim.script.append({"content_tool_call": {"name": "call_service", "arguments": good}})
    llm_sim.script.append({"text": "Done."})
    outcome = benchkit.drive(benchkit.run_scenario(DOC, scenario, {}), _http_chat(llm_server.url))
    assert outcome["passed"] and outcome["json_valid"] is False

    llm_sim.script.append({"status": 500})
    outcome = benchkit.drive(benchkit.run_scenario(DOC, scenario, {}), _http_chat(llm_server.url))
    assert not outcome["passed"] and outcome["failed_request"] and "HTTP 500" in outcome["reason"]

    llm_sim.script.extend(
        [{"tool_calls": [{"name": "get_state", "arguments": {"entity_id": "light.living_room_lamp"}}]}] * 10
    )
    outcome = benchkit.drive(benchkit.run_scenario(DOC, scenario, {}), _http_chat(llm_server.url))
    assert not outcome["passed"] and "still calling tools" in outcome["reason"]


def test_eval_gives_up_when_the_server_keeps_failing(llm_sim, llm_server):
    llm_sim.script.extend([{"status": 500}] * 200)
    parts = benchkit.drive(benchkit.bench_plan(DOC, {}, quick=True), _http_chat(llm_server.url))
    assert parts["toolcall"]["passed"] == 0 and parts["speed"]["decode_tps"] is None
    assert (
        sum("not run" in f["reason"] for f in parts["toolcall"]["failures"])
        == 12 - benchkit.ABORT_AFTER_ERRORS
    )
    assert parts["errors"]


def test_soak_and_resource_tracker(llm_server):
    ticks = iter(range(0, 10_000, 20))
    plan = benchkit.bench_plan(DOC, {}, quick=True, soak_minutes=1, clock=lambda: next(ticks))
    soak = benchkit.drive(plan, _http_chat(llm_server.url))["soak"]
    assert soak["requests"] >= 2 and soak["decode_tps_min"] == 20.0

    tracker = benchkit.ResourceTracker()
    tracker.add(
        {"ram_used_mb": 4000, "ram_total_mb": 7600, "swap_used_mb": 0, "temp_tj_c": 50, "power_mw": 9000}
    )
    tracker.add(
        {
            "ram_used_mb": 6900,
            "ram_total_mb": 7600,
            "swap_used_mb": 300,
            "temp_gpu_c": 71.5,
            "throttled": True,
        }
    )
    tracker.add({"ram_used_mb": None, "raw": "garbage"})
    tracker.add(None)
    assert tracker.summary() == {
        "ram_peak_mb": 6900,
        "ram_total_mb": 7600,
        "ram_free_min_mb": 700,
        "swap_peak_mb": 300,
        "temp_max_c": 71.5,
        "throttled": True,
        "power_avg_mw": 9000,
    }


def test_profiles_catalogue():
    names = [p["name"] for p in profiles.PROFILES]
    assert names[0] == profiles.DEFAULT_PROFILE and len(names) == len(set(names))
    assert "gemma4-e2b-compare" in names, "the Gemma 4 E2B comparison profile of the plan"
    variant = profiles.get_profile("qwen35-4b-balanced@pp0")
    assert variant["presence_penalty"] == 0 and variant["name"] == "qwen35-4b-balanced@pp0"
    assert profiles.get_profile("qwen35-4b-balanced")["presence_penalty"] == 1.5, "variants do not leak"
    assert profiles.get_profile("nope") is None and profiles.get_profile(None) is None
    assert profiles.sampling(None)["temperature"] == 0.7
    assert profiles.sampling(profiles.get_profile("gemma4-e2b-compare"), 0.5)["presence_penalty"] == 0.5
