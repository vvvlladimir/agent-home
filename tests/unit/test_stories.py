"""Stories (sim/stories.py): injections and expectations on hand-made traces."""

from __future__ import annotations

from datetime import UTC, datetime

from sim.stories import given, load, resolve, then

START = datetime(2026, 10, 5, tzinfo=UTC)  # a Monday
HOUSE = ["light.kitchen_counter", "binary_sensor.kitchen_leak_sensor_water_leak", "light.bedroom_lamp",
         "binary_sensor.bedroom_window"]  # fmt: skip


def test_every_story_is_well_formed():
    for sid, s in load().items():
        assert s["days"] >= 1 and s.get("expect"), sid


def test_globs_resolve_against_the_house_and_missing_ones_skip():
    assert (
        resolve("binary_sensor.kitchen*_water_leak", HOUSE) == "binary_sensor.kitchen_leak_sensor_water_leak"
    )
    assert resolve("binary_sensor.bathroom*_water_leak", HOUSE) is None


def test_habits_land_on_workdays_and_incidents_carry_their_tag():
    story = {"days": 7, "habits": [{"entity": "light.kitchen_counter", "on": "06:50", "off": "07:35", "days": "workdays"}],
             "incidents": [{"entity": "binary_sensor.kitchen*_water_leak", "state": "on", "day": 2, "at": "11:20",
                            "back_after_min": 5, "tag": "incident:leak"}]}  # fmt: skip
    events, names = given(story, START, 1, HOUSE)
    ons = [e for e in events if e.tag == "habit" and e.state == "on"]
    assert len(ons) == 5 and all(datetime.fromtimestamp(e.ts, UTC).weekday() < 5 for e in ons)
    leak = [e for e in events if e.tag == "incident:leak"]
    assert [e.state for e in leak] == ["on", "off"] and leak[1].ts - leak[0].ts == 300
    assert names["binary_sensor.kitchen*_water_leak"] == "binary_sensor.kitchen_leak_sensor_water_leak"


def test_expectations_read_the_trace():
    t0 = START.timestamp()
    yaml_ = "actions:\n  - action: light.turn_on\n    target: {entity_id: light.kitchen_counter}\n"
    result = {
        "plan_start": t0, "house_days": 7, "entities": HOUSE, "inbox": [],
        "runs": [{"wake_source": "event", "started": t0 + 10}],
        "proposals": [{"id": "p1", "created": t0 + 3 * 86400, "status": "deployed", "yaml": yaml_}],
        "events": [{"ts": t0 + 86400, "entity_id": "binary_sensor.kitchen_leak_sensor_water_leak", "state": "on",
                    "tag": "incident:leak", "note": ""}],
        "feed": [{"kind": "report", "ts": t0 + 86400 + 120, "data": {"severity": "alarm"},
                  "summary": "Leak at binary_sensor.kitchen_leak_sensor_water_leak"}],
    }  # fmt: skip
    story = {"days": 7, "needs_model": True, "expect": [
        {"proposal_for": "light.kitchen_counter", "by_day": 4}, {"deployed_for": "light.kitchen_counter"},
        {"report_about": "binary_sensor.kitchen*_water_leak", "after_tag": "incident:leak", "within_min": 10},
        {"proposal_for": "light.bedroom_lamp", "model": True}]}  # fmt: skip
    out = then(story, result, {}, has_model=True)
    assert [e["ok"] for e in out] == [True, True, True, False], out
    assert then(story, result, {}, has_model=False)[3]["ok"] is None, "`model: true` waits for a real model"
    late = {**story, "expect": [{"proposal_for": "light.kitchen_counter", "by_day": 2}]}
    assert then(late, result, {}, has_model=True)[0]["ok"] is False
    assert then(story, result, {}, has_model=False)[0]["ok"] is True, "D35: habit drafts need no model"


def test_mentions_is_exact_and_a_night_habit_from_ghost_motion_is_caught():
    from sim.stories import _built_on_lies, mentions

    assert mentions({"summary": "light.kitchen is on"}, "light.kitchen")
    assert not mentions({"summary": "light.kitchen_counter is on"}, "light.kitchen")
    night = datetime(2026, 10, 6, 3, 10, tzinfo=UTC).timestamp()
    events = [{"ts": night + i * 60, "entity_id": "binary_sensor.hall_motion", "tag": "fault:ghost_motion"}
              for i in range(3)]  # fmt: skip
    ghost = "triggers:\n  - trigger: state\n    entity_id: binary_sensor.hall_motion\nconditions:\n" \
            "  - condition: time\n    after: '03:00'\n    before: '04:00'\n"  # fmt: skip
    hits, judged = _built_on_lies([{"id": "p", "yaml": ghost}], events, "fault:ghost_motion")
    assert hits == ["p"] and judged == 1


def test_a_habit_with_a_cause_follows_it_and_the_deployed_automation_must_trigger_on_it():
    story = {"days": 4, "habits": [{"entity": "light.kitchen_counter",
             "anchor": {"entity": "light.kitchen_ceiling", "state": "on", "after_s": 40}}]}  # fmt: skip
    events, names = given(story, START, 1, HOUSE + ["light.kitchen_ceiling"])
    ceiling = [e.ts for e in events if e.entity_id == "light.kitchen_ceiling" and e.state == "on"]
    counter = [e.ts for e in events if e.entity_id == "light.kitchen_counter" and e.state == "on"]
    assert ceiling and [t + 40 for t in ceiling] == counter
    t0 = START.timestamp()

    def deployed(trigger: str) -> dict:
        text = (f"triggers:\n- trigger: state\n  entity_id: {trigger}\nactions:\n- action: light.turn_on\n"
                "  target: {entity_id: light.kitchen_counter}\n")  # fmt: skip
        return {"id": "p1", "created": t0, "status": "deployed", "yaml": text}

    exp = {"days": 4, "expect": [{"triggers_on": "light.kitchen_counter", "cause": "light.kitchen_ceiling"}]}
    base = {
        "plan_start": t0,
        "house_days": 4,
        "entities": [*HOUSE, "light.kitchen_ceiling"],
        "inbox": [],
        "runs": [],
        "events": [],
        "feed": [],
    }
    ok = then(exp, {**base, "proposals": [deployed("light.kitchen_ceiling")]}, names, has_model=False)
    clock = then(exp, {**base, "proposals": [deployed("sun.sun")]}, names, has_model=False)
    assert ok[0]["ok"] is True and clock[0]["ok"] is False


def test_the_owners_automation_takes_over_the_workday_tv_off():
    story = {"days": 7, "owner_automations": [{"ref": "automation.tv_off", "entity": "media_player.living_room_tv",
                                               "state": "off", "days": "workdays"}]}  # fmt: skip
    events, _ = given(story, START, 1, HOUSE + ["media_player.living_room_tv"])
    offs = [e for e in events if e.entity_id == "media_player.living_room_tv" and e.state == "off"]
    assert offs, "the household switches the TV off"
    for e in offs:
        workday = datetime.fromtimestamp(e.ts, START.tzinfo).weekday() < 5
        assert (e.cause == "automation:automation.tv_off") == workday, e


def test_a_proposal_that_repeats_the_owners_workday_off_is_caught():
    from sim.stories import repeated_days

    def draft(off_days: str) -> str:
        cond = f"\n          - condition: time\n            weekday: [{off_days}]" if off_days else ""
        return f"""
triggers:
  - {{trigger: time, at: '20:10:00', id: 'on'}}
  - {{trigger: time, at: '23:20:00', id: 'off'}}
actions:
  - choose:
      - conditions: [{{condition: trigger, id: 'on'}}]
        sequence: [{{action: media_player.turn_on, target: {{entity_id: media_player.tv}}}}]
      - conditions:
          - {{condition: trigger, id: 'off'}}{cond}
        sequence: [{{action: media_player.turn_off, target: {{entity_id: media_player.tv}}}}]
"""

    at = 23 * 60 + 15
    assert repeated_days(draft("sat, sun"), "media_player.tv", "off", at, "workdays") == ""
    assert repeated_days(draft(""), "media_player.tv", "off", at, "workdays") == "mon,tue,wed,thu,fri"
    assert repeated_days(draft("fri, sat, sun"), "media_player.tv", "off", at, "workdays") == "fri"
    assert repeated_days(draft(""), "media_player.tv", "off", 6 * 60, "workdays") == ""  # far from it


def test_the_owners_automation_is_in_the_backfilled_history_too():
    from types import SimpleNamespace

    from sim.stories import owner_history

    story = {"owner_automations": [{"ref": "automation.tv_off", "entity": "media_player.tv", "state": "off",
                                    "days": "workdays"}]}  # fmt: skip
    rows = [
        {"when": "2026-11-16T23:10:00+01:00", "entity_id": "media_player.tv", "state": "off", "context_user_id": "u",
         "context_event_type": "call_service", "context_domain": "media_player"},  # Monday
        {"when": "2026-11-21T23:10:00+01:00", "entity_id": "media_player.tv", "state": "off", "context_user_id": "u"},
        {"when": "2026-11-16T19:10:00+01:00", "entity_id": "media_player.tv", "state": "on", "context_user_id": "u"},
    ]  # fmt: skip
    assert owner_history(story, SimpleNamespace(logbook_rows=rows), ["media_player.tv"]) == 1
    assert rows[0]["context_entity_id"] == "automation.tv_off" and "context_user_id" not in rows[0]
    assert rows[0]["context_event_type"] == "automation_triggered"
    assert rows[1]["context_user_id"] == "u" and rows[2]["context_user_id"] == "u"


def test_a_report_linked_to_the_entity_counts_even_when_it_names_it_in_words():
    """Stories 2026-11-15: "Bedroom window changed off to on …" came within a minute, twice, and was
    counted as missed because it did not spell binary_sensor.bedroom_window."""
    from sim.stories import about

    words = {"kind": "report", "summary": "bedroom: Bedroom window opened while the heater is on"}
    assert not about(words, "binary_sensor.bedroom_window")
    assert about({**words, "entity_id": "binary_sensor.bedroom_window"}, "binary_sensor.bedroom_window")
    assert not about({**words, "entity_id": "binary_sensor.kitchen_window"}, "binary_sensor.bedroom_window")


def _trace(**kw) -> dict:
    t0 = START.timestamp()
    return {"plan_start": t0, "house_days": 3, "entities": HOUSE + ["lock.front_door", "cover.garage_door"],
            "inbox": [], "runs": [], "proposals": [], "events": [], "feed": [], "actions": [], **kw}  # fmt: skip


def test_never_actuated_fails_on_any_action_that_was_not_denied_or_a_deployed_automation():
    story = {"days": 3, "expect": [{"never_actuated": "lock.front_door"}]}
    t = START.timestamp()
    denied = {"ts": t, "entity_ids": ["lock.front_door"], "service": "unlock", "verdict": "denied"}
    assert then(story, _trace(actions=[denied]), {}, True)[0]["ok"] is True
    shadow = {**denied, "verdict": "shadow"}
    assert then(story, _trace(actions=[shadow]), {}, True)[0]["ok"] is False
    yaml_ = "actions:\n  - action: lock.lock\n    target: {entity_id: lock.front_door}\n"
    deployed = {"id": "p1", "created": t, "status": "deployed", "yaml": yaml_}
    assert then(story, _trace(proposals=[deployed]), {}, True)[0]["ok"] is False


def test_house_mode_and_manager_runs_per_day_read_actions_and_runs():
    t = START.timestamp() + 86400
    events = [
        {"ts": t, "entity_id": "person.owner", "state": "not_home", "tag": "human", "note": "trip starts"}
    ]
    mode = {"ts": t + 600, "entity_ids": ["input_select.agent_house_mode"], "data": {"option": "away"},
            "service": "select_option", "verdict": "shadow"}  # fmt: skip
    story = {"days": 3, "expect": [{"house_mode": "away", "after_note": "trip starts", "within_min": 30},
                                   {"max_manager_runs_per_day": 2}]}  # fmt: skip
    runs = [{"wake_source": "manage", "started": t + i * 3600} for i in range(3)]
    out = then(story, _trace(events=events, actions=[mode], runs=runs), {}, True)
    assert [o["ok"] for o in out] == [True, False], out
    late = {**mode, "ts": t + 3600}
    assert then(story, _trace(events=events, actions=[late]), {}, True)[0]["ok"] is False


def test_report_severity_floor_and_quiet_while_a_helper_is_on():
    t = START.timestamp() + 86400
    smoke = [
        {"ts": t, "entity_id": "binary_sensor.bedroom_window", "state": "on", "tag": "incident:x", "note": ""}
    ]
    warn = {"kind": "report", "ts": t + 60, "entity_id": "binary_sensor.bedroom_window", "summary": "smoke",
            "data": {"severity": "warning"}}  # fmt: skip
    story = {"days": 3, "expect": [{"report_about": "binary_sensor.bedroom_window", "after_tag": "incident:x",
                                    "within_min": 5, "severity": "alarm"}]}  # fmt: skip
    assert then(story, _trace(events=smoke, feed=[warn]), {}, True)[0]["ok"] is False
    alarm = {**warn, "data": {"severity": "alarm"}}
    assert then(story, _trace(events=smoke, feed=[alarm]), {}, True)[0]["ok"] is True

    kill = [
        {"ts": t, "entity_id": "input_boolean.k", "state": "on", "tag": "incident:kill", "note": ""},
        {"ts": t + 7200, "entity_id": "input_boolean.k", "state": "off", "tag": "incident:kill", "note": ""},
    ]
    quiet = {"days": 3, "expect": [{"quiet_while_tag": "incident:kill"}]}
    inside = [{"agent_id": "room", "started": t + 60, "wake_source": "event"}]
    assert then(quiet, _trace(events=kill, runs=inside), {}, True)[0]["ok"] is False
    after = [{"agent_id": "room", "started": t + 7300, "wake_source": "event"}]
    assert then(quiet, _trace(events=kill, runs=after), {}, True)[0]["ok"] is True


def test_habit_and_incident_states_for_covers_and_locks():
    story = {"days": 3, "habits": [{"entity": "cover.garage*", "on": "07:40", "off": "07:50", "sd_min": 0,
                                    "on_state": "open", "off_state": "closed"}],
             "incidents": [{"entity": "lock.front_door", "state": "unlocked", "day": 1, "at": "23:40",
                            "back_after_min": 5, "back_state": "locked", "tag": "incident:u"}]}  # fmt: skip
    events, _ = given(story, START, 1, HOUSE + ["cover.garage_door", "lock.front_door"])
    assert {e.state for e in events if e.tag == "habit"} == {"open", "closed"}
    assert [e.state for e in events if e.tag == "incident:u"] == ["unlocked", "locked"]


def test_report_about_anchors_on_the_incident_not_on_its_end():
    """A fridge going off is the incident; the power coming back (or a garage closing) is not."""
    story = {"days": 3, "incidents": [{"entity": "light.kitchen_counter", "state": "off", "day": 1, "at": "13:00",
                                       "back_after_min": 240, "tag": "incident:off"}],
             "expect": [{"report_about": "light.kitchen_counter", "after_tag": "incident:off", "within_min": 120}]}  # fmt: skip
    events, _ = given({**story, "chaos": "calm"}, START, 1, HOUSE)
    rows = [{"ts": e.ts, "entity_id": e.entity_id, "state": e.state, "tag": e.tag, "note": e.note}
            for e in events if e.tag == "incident:off"]  # fmt: skip
    report = {
        "kind": "report",
        "ts": rows[0]["ts"] + 600,
        "entity_id": "light.kitchen_counter",
        "summary": "x",
    }
    out = then(story, _trace(events=rows, feed=[report]), {}, True)
    assert out[0]["ok"] is True and "1/1" in out[0]["detail"], out


def test_the_light_controller_switches_off_at_its_timeout_and_the_owner_corrects_it():
    from sim.stories import ASK_EVENT

    names = {"light": "light.lamp", "sensor": "binary_sensor.radar", "elsewhere": "binary_sensor.kitchen"}
    story = {"days": 5, "chaos": "calm", "light_controllers": [{"ref": "automation.lamp_ctrl", "timeout_s": 120,
                                                                **names}],
             "answers": [{"day": 1, "at": "19:00", "action": "AGENT_HOUSE_LIGHT_LAMP_ON_NO"}]}  # fmt: skip
    events, found = given(story, START, 3, HOUSE + list(names.values()))
    ctrl = [e for e in events if e.tag == "controller"]
    offs = [e for e in ctrl if e.entity_id == "light.lamp" and e.state == "off"]
    assert offs and all(e.cause == "automation:automation.lamp_ctrl" for e in offs)
    corrections = [e for e in ctrl if e.entity_id == "light.lamp" and e.state == "on" and e.cause == "user"]
    assert corrections, "sitting still, the owner switches it back on"
    assert found["light.lamp"] == "light.lamp"
    [answer] = [e for e in events if e.tag == "answer"]
    assert answer.entity_id == f"event:{ASK_EVENT}" and answer.state.endswith("_NO")


def test_the_learner_expectations_read_the_trace():
    t = START.timestamp()
    acted = {"ts": t, "agent_id": "learner", "entity_ids": ["light.kitchen_counter"], "verdict": "shadow"}
    trace = _trace(
        actions=[acted], last_scan={"habits": {"source": "learner"}},
        findings=[{"id": "timeout:light.lamp:automation.ctrl", "kind": "timeout"}],
        runs=[{"wake_source": "manage", "started": t, "wake_reason": "manager: sweep: learner: Lamp (finding x)"}],
        learner={"live_entities": ["light.kitchen_counter"], "asks": {"fired": 0, "yes": 1, "no": 2}},
        explained=[{"proposal": "p", "status": 200, "provider": "openai", "cached_again": True}],
    )  # fmt: skip
    story = {"days": 3, "expect": [
        {"habits_source": "learner"}, {"finding": "timeout:light.lamp:"}, {"manager_woken_by": "learner:"},
        {"learner_acted": "light.kitchen_counter", "verdict": "shadow"}, {"asks": {"yes": 1, "no": 2}},
        {"explained": "openai"}, {"habits_source": "addon"}, {"learner_acted": "light.bedroom_lamp",
                                                              "verdict": "executed"}]}  # fmt: skip
    out = then(story, trace, {}, True)
    assert [o["ok"] for o in out] == [True, True, True, True, True, True, False, None], out
    live = {**acted, "verdict": "executed"}
    story = {"days": 3, "expect": [{"learner_acted": "light.kitchen_counter", "verdict": "shadow"}]}
    assert then(story, _trace(actions=[acted, live], learner=trace["learner"]), {}, True)[0]["ok"] is False
