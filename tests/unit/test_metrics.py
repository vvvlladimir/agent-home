"""D28 accuracy metrics: outcomes per automation / agent, Wilson intervals, the pooled house score."""

from __future__ import annotations

import pytest

from app.core.insights import UNDO_S, Change
from app.core.metrics import MIN_N, evaluate, f1, intended_states, is_weak, rate, wilson

AUTO = "automation.agent_house_lamp"
OURS = {AUTO: {"ha_id": "agent_house_lamp", "since": 0.0, "title": "Lamp", "version": 1}}
INTENTS = {"agent_house_lamp": {"light.lamp": {"on"}}}
HOME = Change(0.0, "person.owner", "home", "other")
AWAY = "not_home"


def auto(ts: float, state: str = "on") -> Change:
    return Change(ts, "light.lamp", state, "automation", AUTO)


def hand(ts: float, state: str) -> Change:
    return Change(ts, "light.lamp", state, "user")


def test_wilson_matches_the_published_values():
    # Newcombe (1998), table I: 81/263 → 0.2553–0.3662; and the edge 0/10 → 0–0.2775
    low, high = wilson(81, 263)
    assert low == pytest.approx(0.2553, abs=1e-4) and high == pytest.approx(0.3662, abs=1e-4)
    low, high = wilson(0, 10)
    assert low == 0.0 and high == pytest.approx(0.2775, abs=1e-4)
    assert wilson(0, 0) is None


def test_below_min_n_a_rate_is_insufficient_not_zero_or_one():
    small = rate(MIN_N - 1, MIN_N - 1)
    assert small["value"] is None and small["low"] is None and small["n"] == MIN_N - 1
    assert rate(MIN_N, MIN_N)["value"] == 1.0
    assert f1(None, 0.9) is None and f1(0.0, 0.0) == 0.0


def test_kept_undone_unobserved_and_missed_are_told_apart():
    day = 86400.0
    changes = [HOME]
    for i in range(6):  # six nights: on by the automation, kept
        changes += [auto(i * day + 100), hand(i * day + 100 + UNDO_S + 60, "off")]
    changes += [auto(7 * day), hand(7 * day + 30, "off")]  # undone within the window
    changes += [hand(8 * day, "on")]  # done by hand, the automation did not: missed
    changes += [Change(9 * day - 10, "person.owner", AWAY, "other"), auto(9 * day)]  # nobody home
    changes.sort(key=lambda c: c.ts)
    result = evaluate(changes, OURS, INTENTS)
    [item] = result["automations"]
    assert (item["kept"], item["undone"], item["missed"], item["unobserved"]) == (6, 1, 1, 1)
    assert item["precision"]["value"] == round(6 / 7, 3)
    assert item["coverage"]["value"] == round(6 / 7, 3)
    assert item["precision"]["low"] < item["precision"]["value"] < item["precision"]["high"]
    program = result["program"]
    assert program["score"] == f1(program["precision"]["value"], program["coverage"]["value"])
    assert program["score_low"] < program["score"]
    assert program["counts"]["unobserved"] == 1


def test_an_owner_repeating_what_the_automation_just_did_is_not_a_miss():
    changes = [HOME, auto(100), hand(130, "on")]
    [item] = evaluate(changes, OURS, INTENTS)["automations"]
    assert item["missed"] == 0 and item["kept"] == 1


def test_changes_before_the_current_version_do_not_count():
    ours = {AUTO: {**OURS[AUTO], "since": 1000.0}}
    [item] = evaluate([HOME, auto(10), hand(20, "off"), hand(500, "on")], ours, INTENTS)["automations"]
    assert (item["kept"], item["undone"], item["missed"]) == (0, 0, 0)


def test_agent_actions_count_for_precision_and_the_pooled_house_score():
    changes = [
        HOME,
        Change(10, "light.desk", "on", "agent", "office"),
        Change(40, "light.desk", "off", "user"),
    ]
    result = evaluate(changes, {}, {})
    [agent] = result["agents"]
    assert (agent["agent_id"], agent["kept"], agent["undone"]) == ("office", 0, 1)
    assert result["program"]["counts"]["undone"] == 1
    assert result["program"]["coverage"]["n"] == 0  # no automation, no coverage


def test_weak_is_the_point_estimate_so_a_young_perfect_automation_is_not_flagged():
    perfect = {"precision": rate(5, 5), "coverage": rate(5, 5)}
    assert perfect["precision"]["low"] < 0.6 and not is_weak(perfect)
    assert is_weak({"precision": rate(2, 6), "coverage": rate(5, 5)})
    assert not is_weak({"precision": rate(1, 3), "coverage": rate(1, 3)})  # too little data


def test_intended_states_reads_targets_through_choose_and_service_data():
    text = """alias: x
triggers: [{trigger: time, at: "07:00"}]
actions:
  - choose:
      - conditions: []
        sequence: [{action: light.turn_on, target: {entity_id: [light.a, light.b]}}]
  - action: climate.set_hvac_mode
    target: {entity_id: climate.room}
    data: {hvac_mode: heat}
  - action: light.toggle
    target: {entity_id: light.c}
"""
    assert intended_states(text) == {"light.a": {"on"}, "light.b": {"on"}, "climate.room": {"heat"}}
    assert intended_states(": not yaml [") == {}


def test_outcomes_of_an_earlier_version_stay_in_the_house_counts_and_show_per_version():
    """QA-001: approving v2 must not make v1's undos vanish from the headline numbers."""
    ours = {AUTO: {**OURS[AUTO], "version": 2, "since": 1000.0,
                   "previous": [{"version": 1, "since": 0.0, "intents": INTENTS["agent_house_lamp"]}]}}  # fmt: skip
    changes = [HOME]
    for i in range(5):  # v1 acted five times, undone each time
        changes += [auto(10 + i * 60), hand(20 + i * 60, "off")]
    changes += [auto(2000)]  # v2 acted once, kept
    result = evaluate(changes, ours, INTENTS)
    [item] = result["automations"]
    assert (item["version"], item["kept"], item["undone"]) == (2, 1, 0)
    [v1] = item["previous"]
    assert (v1["version"], v1["kept"], v1["undone"]) == (1, 0, 5)
    assert v1["precision"]["value"] == 0.0 and v1["precision"]["n"] == 5
    assert result["program"]["counts"]["undone"] == 5 and result["program"]["counts"]["kept"] == 1
