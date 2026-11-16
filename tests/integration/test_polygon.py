"""The polygon end to end (fake clouds, the real learner in-process, local ports) and its invariant
checks on hand-made data."""

from __future__ import annotations

import json

import pytest

from sim import polygon
from sim.run import Ports


def _run(tmp_path, *extra: str) -> tuple[int, dict]:
    out = tmp_path / "out"
    rc = polygon.main(
        [
            "run",
            "--speed",
            "50",
            "--settle",
            "3",
            "--rate",
            "0",
            "--timeout",
            "90",
            "--out-dir",
            str(out),
            "--sim-config",
            str(tmp_path / "no-sim-config"),
            *extra,
        ]
    )
    return rc, json.loads((out / "report.json").read_text())


def test_shadow_scenario_passes_and_reports(tmp_path):
    rc, report = _run(tmp_path, "--scenario", "window_while_heating")
    assert rc == 0 and report["ok"], [c for c in report["scenarios"][0]["checks"] if not c["ok"]]
    s = report["scenarios"][0]
    assert [(r["agent_id"], r["wake_source"]) for r in s["runs"]] == [("orchestrator", "manage")]
    assert all(r["trace"] for r in s["runs"])  # full traces, not summaries
    assert s["ha_calls"] == []
    assert [a["verdict"] for a in s["actions"]] == ["denied"], "the scripted lock unlock"
    assert s["learner"]["connected"] and "binary_sensor.bedroom_window" in s["learner"]["entities"]
    assert {c["name"] for c in s["checks"]} >= {"learner_link_ok", "learner_events", "known_actors_only"}
    assert report["meta"]["mode"] == "shadow" and report["meta"]["backends"]["learner"] == "sim"


def test_live_scenario_actuates_through_policy(tmp_path):
    rc, report = _run(
        tmp_path, "--scenario", "arrive_home", "--live", "--speed", "10"
    )  # motion stays on long enough
    assert rc == 0
    s = report["scenarios"][0]
    assert any(a["verdict"] == "executed" and a["agent_id"] == "orchestrator" for a in s["actions"])
    assert s["ha_calls"], "the house manager switches the hallway light, live, through the policy"
    assert {c["name"] for c in s["checks"]} >= {"calls_match_actions", "t3_never_actuated", "expected_actions"}


def test_real_backend_without_key_fails_setup(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rc = polygon.main(
        [
            "run",
            "--scenario",
            "evening",
            "--orchestrator",
            "openai",
            "--out-dir",
            str(tmp_path),
            "--sim-config",
            str(tmp_path / "none"),
        ]
    )
    assert rc == 2


def test_ports_pick():
    assert Ports.pick(None).ha == 8123 and Ports.pick(None, 9000).ingress == 9001
    assert list(vars(Ports.pick(20000)).values()) == list(range(20000, 20007))
    free = Ports.pick(0)
    assert len(set(vars(free).values())) == 7


def _result(**over) -> dict:
    base = {
        "timed_out": False,
        "actions": [],
        "ha_calls": [],
        "ha_config_writes": [],
        "runs": [],
        "owner_automations_changed": False,
        "llm_requests": [],
        "entities": ["light.a"],
    }
    return {**base, **over}


def _failed(result: dict, mode: str = "shadow", expect: dict | None = None) -> set[str]:
    return {c["name"] for c in polygon.check(result, mode, expect or {}) if not c["ok"]}


def _action(**over) -> dict:
    return {"id": "a1", "tier": "T0", "verdict": "shadow", "entity_ids": ["light.a"], "agent_id": "orchestrator",
            **over}


@pytest.mark.parametrize(
    ("result", "mode", "expect", "failed"),
    [
        (_result(), "shadow", {}, set()),
        (_result(ha_calls=[{"domain": "light"}]), "shadow", {}, {"shadow_no_service_calls"}),
        (_result(actions=[_action(verdict="executed")]), "shadow", {}, {"shadow_no_executed_actions"}),
        (
            _result(actions=[_action(tier="T3", verdict="executed")], ha_calls=[{}]),
            "live",
            {},
            {"t3_never_actuated"},
        ),
        (
            _result(ha_calls=[{}, {}], actions=[_action(verdict="executed")]),
            "live",
            {},
            {"calls_match_actions"},
        ),
        (_result(ha_config_writes=[{"id": "owner_lights"}]), "shadow", {}, {"owner_automations_untouched"}),
        (_result(ha_config_writes=[{"id": "agent_house_x"}]), "shadow", {}, set()),
        (_result(owner_automations_changed=True), "shadow", {}, {"owner_automations_untouched"}),
        (_result(actions=[_action(entity_ids=["light.ghost"])]), "shadow", {}, {"known_entities_only"}),
        (
            _result(runs=[{"id": "r", "agent_id": "x", "status": "error", "wake_source": "event"}]),
            "shadow",
            {},
            {"runs_without_errors"},
        ),
        (_result(llm_requests=[{"status": 429}]), "shadow", {}, {"llm_requests_ok"}),
        (_result(timed_out=True), "shadow", {}, {"scenario_finished"}),
        (_result(actions=[_action(agent_id="bedroom")]), "shadow", {}, {"known_actors_only"}),
        (_result(actions=[_action(agent_id="learner")]), "shadow", {}, set()),
        (_result(), "live", {"actions": ["light.a"]}, {"expected_actions"}),
        (_result(), "shadow", {"actions": ["light.a"]}, set()),
        (
            _result(learner={"backend": "sim", "connected": True, "dropped": 0, "entities": []}),
            "shadow", {"learner_events": ["light.a"]}, {"learner_events"},
        ),
        (_result(learner={"backend": "sim", "connected": False, "dropped": 0}), "shadow", {}, {"learner_link_ok"}),
    ],
)
def test_checks_catch_violations(result, mode, expect, failed):
    assert _failed(result, mode, expect) == failed
