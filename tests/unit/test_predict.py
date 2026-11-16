"""D35: habits are predictions. A causal household (tests/unit/predict_house.py): what has a cause is
proposed, what is a choice or only a clock is not, without any rule per domain."""

from __future__ import annotations

import time

import yaml

from app.core import predict
from app.core.insights import Change
from app.core.lifecycle import to_yaml
from app.core.policy import DEFAULT_POLICY
from tests.unit.predict_house import at, house, household


def scan(changes, **kw):
    kw = {"days": 14, "min_days": 3, "policy": DEFAULT_POLICY, **kw}
    return predict.scan(changes, house(), **kw)


def by_key(preds):
    return {(p["entity_id"], p["state"]): p for p in preds}


def test_what_has_a_cause_is_proposed_and_choices_and_clocks_are_not():
    preds, funnel = scan(household())
    found = by_key(preds)
    hall = found[("light.hallway", "on")]
    assert hall["mode"] == "act" and hall["p"] >= 0.8 and hall["right_days"] >= 3
    assert "binary_sensor.hall_motion is on" in hall["rule"]
    assert ("media_player.tv", "on") not in found, "watching TV is a choice: unpredictable, so never proposed"
    assert ("light.kitchen", "on") not in found, "a clock with nothing causing it is not acted on"
    assert "media_player.tv → on" in funnel["waiting"] and funnel["predictions"] == len(preds)


def test_the_rule_becomes_one_automation_with_its_trigger_and_conditions():
    [hall] = [p for p in scan(household())[0] if (p["entity_id"], p["state"]) == ("light.hallway", "on")]
    config = hall["yaml_config"]
    assert config["triggers"][0] == {"trigger": "state", "entity_id": "binary_sensor.hall_motion", "to": "on"}
    assert {"condition": "not", "conditions": [{"condition": "state", "entity_id": "light.hallway",
                                                 "state": "on"}]} in config["conditions"]  # fmt: skip
    assert config["actions"] == [{"action": "light.turn_on", "target": {"entity_id": "light.hallway"}}]
    assert yaml.safe_load(to_yaml(config)) == config
    assert "On days the rule had not seen" in predict.request(hall)


def test_a_device_the_owner_always_switches_back_after_a_while_gets_a_hold_rule():
    found = by_key(scan(household())[0])
    off = found[("light.hallway", "off")]
    assert off["path"] == [] and off["other"] == "on" and 1 <= off["hold_min"] <= 3
    trig = off["yaml_config"]["triggers"][0]
    assert trig == {
        "trigger": "state",
        "entity_id": "light.hallway",
        "to": "on",
        "for": {"minutes": off["hold_min"]},
    }


def test_a_costly_mistake_makes_it_ask_first_or_wait(monkeypatch):
    changes = household()
    assert by_key(scan(changes)[0])[("light.hallway", "on")]["mode"] == "act"
    # ours switched the light on at noon and the owner undid it within 10 s, ten times
    ours = {"automation.agent_house_hall": {"ha_id": "agent_house_hall", "targets": set(), "ask": False}}
    extra = []
    for d in range(10):
        t = at(d, 12)
        extra += [Change(t, "light.hallway", "on", "automation", "automation.agent_house_hall"),
                  Change(t + 10, "light.hallway", "off", "user")]  # fmt: skip
    changes = sorted(changes + extra, key=lambda c: c.ts)
    kept, costs = predict.feedback(changes, set(ours))
    assert costs[("light.hallway", "on")] == [predict.COST_FAST] * 10 and not [
        k for k in kept if k[2] == "on"
    ]
    hall = by_key(scan(changes, ours=ours)[0])[("light.hallway", "on")]
    assert hall["cost"] > 3.5, "learnt from the undos: acting now needs P > cost / (1 + cost) ≈ 0.79"
    assert hall["mode"] == "act" and hall["p"] > hall["cost"] / (1 + hall["cost"])
    monkeypatch.setattr(predict, "COST_FAST", 40.0)  # an owner for whom a wrong light is a disaster
    hall = by_key(scan(changes, ours=ours)[0]).get(("light.hallway", "on"))
    assert hall is None or hall["mode"] == "ask", "not sure enough to act any more"


def test_asks_stay_within_the_budget(monkeypatch):
    monkeypatch.setattr(predict, "COST_PRIOR", 1000.0)  # nothing is sure enough to act: all would ask
    preds, funnel = scan(household())
    assert preds and all(p["mode"] == "ask" for p in preds)
    assert funnel["asks_per_day"] <= predict.ASK_BUDGET
    ask = preds[0]["yaml_config"]
    assert ask["actions"][0]["action"] == predict.NOTIFY
    answer, refused, by_hand = ask["actions"][1]["wait_for_trigger"]
    assert answer["event_type"] == predict.ASK_EVENT and by_hand["trigger"] == "state", "or the owner does it"
    tag = answer["event_data"]["action"]
    assert refused["event_data"]["action"] == f"{tag}_NO" and refused["id"] == "no", "a No clears it too (D38)"
    buttons = ask["actions"][0]["data"]["data"]["actions"]
    assert [b["action"] for b in buttons] == [tag, f"{tag}_NO"]
    assert ask["actions"][2]["data"]["message"] == "clear_notification", "answered, done by hand or timed out"
    assert "wait.trigger.id == 'yes'" in ask["actions"][3]["value_template"]
    assert ask["actions"][4]["action"].endswith((".turn_on", ".turn_off"))
    monkeypatch.setattr(predict, "ASK_BUDGET", 0.5)
    preds, funnel = scan(household())
    assert funnel["asks_per_day"] <= 0.5 and any("(ask budget)" in w for w in funnel["waiting"])


def test_an_accepted_ask_climbs_to_act_and_supersedes_it(monkeypatch):
    changes = household()
    hand = [c for c in changes if c.entity_id == "light.hallway" and c.state == "on"]
    # our ask automation turned the light on after the owner tapped the button; never undone
    asked = [Change(c.ts, c.entity_id, c.state, "automation", "automation.agent_house_ask") for c in hand]
    changes = sorted([c for c in changes if c not in hand] + asked, key=lambda c: c.ts)
    ours = {
        "automation.agent_house_ask": {
            "ha_id": "agent_house_ask",
            "targets": {("light.hallway", "on")},
            "ask": True,
        }
    }
    found = by_key(scan(changes, ours=ours)[0])
    hall = found[("light.hallway", "on")]
    assert (
        hall["mode"] == "act" and hall["accepted"] == len(hand) and hall["supersedes"] == ["agent_house_ask"]
    )
    monkeypatch.setattr(predict, "COST_PRIOR", 1000.0)
    found = by_key(scan(changes, ours=ours)[0])
    assert ("light.hallway", "on") not in found, "our ask stands until it has earned acting"


def test_the_owners_own_automation_is_not_drafted_again():
    changes = [Change(c.ts, c.entity_id, c.state, "automation", "automation.owner_hall")
               if c.entity_id == "light.hallway" else c for c in household()]  # fmt: skip
    changes += [Change(at(d, 21), "light.hallway", "on", "user") for d in range(3)]
    preds, funnel = scan(sorted(changes, key=lambda c: c.ts))
    assert ("light.hallway", "on") not in by_key(preds)
    assert {"entity_id": "light.hallway", "state": "on", "by": ["automation.owner_hall"]} in funnel[
        "covered_by_owner"
    ]


def test_a_month_of_a_house_is_cheap():
    changes = household(days=30)
    start = time.perf_counter()
    scan(changes, days=30)
    assert time.perf_counter() - start < 10


def test_an_automation_of_ours_for_on_leaves_off_to_be_proposed():
    text = to_yaml({"alias": "x", "triggers": [{"trigger": "state", "entity_id": "binary_sensor.hall_motion"}],
                    "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.hallway"}}]})  # fmt: skip
    assert predict.acted_states(text) == {("light.hallway", "on")}
    ours = {
        "automation.agent_house_x": {
            "ha_id": "agent_house_x",
            "targets": predict.acted_states(text),
            "ask": False,
        }
    }
    found = by_key(scan(household(), ours=ours)[0])
    assert ("light.hallway", "on") not in found and ("light.hallway", "off") in found


def test_an_ask_ignored_and_then_done_by_hand_counts_as_right():
    """The owner did not tap the question but switched the light themselves a few minutes later:
    the question was cleared, and the moment it asked is a positive for the next scan."""
    changes = household()
    ours = {
        "automation.agent_house_ask": {
            "ha_id": "agent_house_ask",
            "targets": {("light.lamp", "on")},
            "ask": True,
        }
    }
    lamp_on = [c.ts for c in changes if c.entity_id == "light.lamp" and c.state == "on"]
    fires = {
        "automation.agent_house_ask": [t - 240 for t in lamp_on] + [at(0, 3)]
    }  # 4 min before; one at 03:00
    did = predict.ask_positives(fires, ours, changes)
    assert len(did[("light.lamp", "on")]) == len(lamp_on), (
        "the 03:00 ask was ignored and not done: no positive"
    )
    preds = by_key(scan(changes, ours=ours, fires=fires)[0])
    assert ("light.lamp", "on") not in preds or preds[("light.lamp", "on")]["did_after_ask"] == len(lamp_on)


def test_the_owners_own_house_mode_switches_are_a_habit_too():
    """The owner sets the house mode to away when they leave and home when they are back: the same
    machinery proposes it (input_select.select_option), with the person as the cause."""
    mode = "input_select.agent_house_mode"
    changes = household()
    for d in range(14):
        out, back = at(d, 8.2), at(d, 17.4)
        changes += [Change(out, "person.owner", "not_home", "physical"), Change(out + 50, mode, "away", "user"),
                    Change(back, "person.owner", "home", "physical"), Change(back + 40, mode, "home", "user")]  # fmt: skip
    ha = house()
    ha.states[mode] = {"state": "home", "attributes": {}}
    preds, _ = predict.scan(
        sorted(changes, key=lambda c: c.ts), ha, days=14, min_days=3, policy=DEFAULT_POLICY
    )
    away = by_key(preds)[(mode, "away")]
    assert away["mode"] == "act" and "person.owner" in away["rule"]
    assert away["yaml_config"]["actions"] == [
        {"action": "input_select.select_option", "target": {"entity_id": mode}, "data": {"option": "away"}}
    ]
    assert predict.acted_states(to_yaml(away["yaml_config"])) == {(mode, "away")}
