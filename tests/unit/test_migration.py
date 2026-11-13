"""M5 importer: the Node-RED reader, the one-line summary and the parity matcher (all deterministic)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.migration import match, summarize
from app.core.nodered import parse_flows

SAMPLES = Path(__file__).resolve().parents[2] / "owner-reports" / "nodered"
FLOW = [
    {"id": "tab1", "type": "tab", "label": "Hall"},
    {"id": "sub1", "type": "subflow", "name": "Lamp", "env": [{"name": "LAMP", "value": "light.default"}]},
    {"id": "s1", "type": "api-call-service", "z": "sub1", "action": "light.turn_on", "entityId": ["${LAMP}"]},
    {"id": "g1", "type": "group", "z": "tab1", "name": "Motion light", "nodes": ["n1", "n2", "n3", "g2"]},
    {"id": "g2", "type": "group", "z": "tab1", "g": "g1", "nodes": ["n4", "n5"]},
    {"id": "n1", "type": "server-state-changed", "z": "tab1", "g": "g1", "ifState": "on",
     "ifStateOperator": "is", "entities": {"entity": ["binary_sensor.hall_motion"]}},
    {"id": "n2", "type": "api-current-state", "z": "tab1", "g": "g1", "entity_id": "sun.sun",
     "halt_if": "below_horizon"},
    {"id": "n3", "type": "subflow:sub1", "z": "tab1", "g": "g1",
     "env": [{"name": "LAMP", "value": "light.hall"}]},
    {"id": "n4", "type": "function", "z": "tab1", "g": "g2", "func": "return msg;"},
    {"id": "n5", "type": "api-call-service", "z": "tab1", "g": "g2", "d": True,
     "action": "lock.unlock", "entityId": ["lock.front_door"]},
    {"id": "n6", "type": "comment", "z": "tab1", "name": "just a note"},
]  # fmt: skip


def test_a_flow_becomes_one_original_per_top_group_with_subflows_expanded():
    [original] = parse_flows(json.dumps(FLOW), "hall")
    assert original["key"] == "hall_g1" and original["title"] == "Hall: Motion light"
    config = original["config"]
    assert config["triggers"] == [
        {"trigger": "state", "entity_id": ["binary_sensor.hall_motion"], "to": "on"}
    ]
    assert config["conditions"] == [{"condition": "state", "entity_id": "sun.sun", "state": "below_horizon"}]
    assert config["actions"] == [{"action": "light.turn_on", "target": {"entity_id": ["light.hall"]}}], (
        "the subflow's ${LAMP} is the instance's lamp; the disabled lock node is left out"
    )
    assert config["logic_nodes"] == {"function": 1}
    assert "Logic not read: 1 function" in summarize(config)


def test_unreadable_exports_are_refused():
    for bad in ("{", '{"a": 1}'):
        with pytest.raises(ValueError):
            parse_flows(bad, "x")
    assert parse_flows("[]", "x") == []


@pytest.mark.parametrize("name", ["bedroom", "livingroom"])
def test_the_owners_exports_parse(name):
    originals = parse_flows((SAMPLES / f"{name}.json").read_text(), name)
    assert len(originals) >= 4
    assert len({o["key"] for o in originals}) == len(originals)
    text = json.dumps([o["config"] for o in originals])
    assert "${" not in json.dumps([o["config"]["actions"] for o in originals]), "env variables are resolved"
    assert "binary_sensor.esphome_radar" in text and "light.turn_on" in text
    assert all(len(json.dumps(o["config"])) < 6000 for o in originals), "small enough for a draft request"


def test_summary_of_an_ha_automation():
    config = {
        "triggers": [{"trigger": "state", "entity_id": "binary_sensor.m", "to": "on"}, {"trigger": "sun"}],
        "actions": [
            {
                "if": [],
                "then": [{"action": "light.turn_on", "target": {"entity_id": ["light.a", "light.b"]}}],
            },
            {"choose": [{"sequence": [{"service": "cover.close_cover", "entity_id": "cover.c"}]}]},
        ],
    }
    assert summarize(config) == (
        "When: binary_sensor.m → on; sun. Does: light.turn_on light.a, light.b; cover.close_cover cover.c"
    )
    assert summarize(None) is None and summarize({}) is None


def test_parity_pairs_firings_within_a_minute():
    old = [{"ts": 100.0, "trigger": "a"}, {"ts": 500.0, "trigger": "b"}, {"ts": 900.0, "trigger": "c"}]
    new = [{"ts": 130.0, "trigger": "x"}, {"ts": 700.0, "trigger": "y"}, {"ts": 905.0, "trigger": "z"}]
    matched, diffs = match(old, new)
    assert matched == 2
    assert [(d["side"], d["ts"]) for d in diffs] == [("replacement_only", 700.0), ("original_only", 500.0)]
    assert match([], new)[0] == 0 and len(match([], new)[1]) == 3
