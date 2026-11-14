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
        {"max_event_wakes_per_day": 5}]}  # fmt: skip
    out = then(story, result, {}, has_model=True)
    assert [e["ok"] for e in out] == [True, True, True, True], out
    late = {**story, "expect": [{"proposal_for": "light.kitchen_counter", "by_day": 2}]}
    assert then(late, result, {}, has_model=True)[0]["ok"] is False
    assert then(story, result, {}, has_model=False)[0]["ok"] is None, "n/a without a model"


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
