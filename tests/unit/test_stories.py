"""Stories (sim/stories.py): injections and expectations on hand-made traces."""

from __future__ import annotations

from datetime import UTC, datetime

from sim.stories import given, load, resolve, schedule_misfit, then

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


def test_schedule_fits_needs_the_habits_times_and_exactly_its_days():
    """Stories run 2026-11-14 #2: a Mon–Fri habit deployed as Mon–Wed passed every check."""
    habit = {"on": "06:50", "off": "07:35", "days": "workdays"}

    def draft(days: str, on: str = "06:51:00") -> str:
        cond = f"conditions:\n- condition: time\n  weekday: [{days}]\n" if days else ""
        return (f"triggers:\n- trigger: time\n  at: '{on}'\n- trigger: time\n  at: '07:36:00'\n"
                f"{cond}actions: []\n")  # fmt: skip

    assert schedule_misfit(draft("mon, tue, wed, thu, fri"), habit) is None
    assert (
        schedule_misfit(draft("mon, tue, wed"), habit) == "runs on mon,tue,wed, habit is mon,tue,wed,thu,fri"
    )
    assert "runs on mon,tue,wed,thu,fri,sat,sun" in schedule_misfit(draft(""), habit)
    assert schedule_misfit(draft("mon, tue, wed, thu, fri", on="09:00"), habit) == (
        "no time trigger near 06:50 (on)"
    )
    assert schedule_misfit(draft(""), {**habit, "days": "all"}) is None


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
