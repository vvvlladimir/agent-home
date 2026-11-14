"""The polygon end to end (fakes only, local ports) and its invariant checks on hand-made data."""

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
    assert rc == 0 and report["ok"]
    s = report["scenarios"][0]
    assert {r["agent_id"] for r in s["runs"] if r["wake_source"] == "event"} == {"bedroom"}
    assert all(r["trace"] for r in s["runs"])  # full traces, not summaries
    assert s["ha_calls"] == []
    assert report["meta"]["mode"] == "shadow" and report["meta"]["backends"]["room"] == "fake"


def test_live_scenario_actuates_through_policy(tmp_path):
    rc, report = _run(
        tmp_path, "--scenario", "arrive_home", "--live", "--speed", "10"
    )  # motion stays on long enough
    assert rc == 0
    s = report["scenarios"][0]
    assert any(a["verdict"] == "executed" for a in s["actions"])
    assert s["ha_calls"], "the fake LLM switches the hallway light on motion"
    assert {c["name"] for c in s["checks"]} >= {"calls_match_actions", "t3_never_actuated"}


def test_real_backend_without_key_fails_setup(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rc = polygon.main(
        [
            "run",
            "--scenario",
            "evening",
            "--room-llm",
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
    assert list(vars(Ports.pick(20000)).values()) == list(range(20000, 20008))
    free = Ports.pick(0)
    assert len(set(vars(free).values())) == 8


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
    return {"id": "a1", "tier": "T0", "verdict": "shadow", "entity_ids": ["light.a"], **over}


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
        (_result(), "shadow", {"wake": ["bedroom"]}, {"expected_wakes"}),
        (
            _result(runs=[{"id": "r", "agent_id": "bedroom", "status": "ok", "wake_source": "event"}]),
            "shadow",
            {"wake": ["bedroom"], "no_wake": ["bedroom"]},
            {"no_unexpected_wakes"},
        ),
    ],
)
def test_checks_catch_violations(result, mode, expect, failed):
    assert _failed(result, mode, expect) == failed
