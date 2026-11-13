"""M5 history analysis: who caused a change, habits, undone automations, automations that fight.
Pure functions over logbook entries shaped like HA's (field names [UNVERIFIED-ON-HW])."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.core.insights import (
    Change,
    changes_from_logbook,
    classify,
    find_conflicts,
    find_habits,
    find_undos,
    habit_request,
)

BASE = datetime(2026, 11, 1, 0, 0).astimezone()
AREAS = {"light.desk": "office", "binary_sensor.desk_zone": "office", "binary_sensor.hall_motion": "hall",
         "light.porch": "porch", "light.hall": "hall"}  # fmt: skip


def at(day: int, hour: int, minute: int, second: int = 0) -> float:
    return (BASE + timedelta(days=day, hours=hour, minutes=minute, seconds=second)).timestamp()


def entry(ts: float, entity_id: str, state: str, **context) -> dict:
    return {"when": datetime.fromtimestamp(ts).astimezone().isoformat(), "entity_id": entity_id,
            "state": state, **context}  # fmt: skip


AUTO = {"context_event_type": "automation_triggered", "context_domain": "automation"}


def test_classify_causes():
    assert classify({}) == ("physical", None)
    assert classify({"context_user_id": "owner"}, own_user="addon") == ("user", None)
    assert classify({"context_user_id": "addon"}, own_user="addon") == ("agent", None)
    assert classify({**AUTO, "context_entity_id": "automation.x"}) == ("automation", "automation.x")
    assert classify({"context_domain": "script", "context_entity_id": "script.y"}) == ("script", "script.y")
    assert classify({"context_event_type": "state_changed", "context_entity_id": "sensor.z"})[0] == "other"


def test_our_actions_are_not_mistaken_for_the_owner():
    rows = [entry(at(0, 9, 0), "light.desk", "on", context_user_id="u"), entry(at(0, 9, 5), "light.desk", "off"),
            entry(at(0, 9, 6), "light.desk", "unavailable"), {"entity_id": "light.desk", "when": "x", "state": "on"}]  # fmt: skip
    changes = changes_from_logbook(
        rows, None, [{"ts": at(0, 9, 0, 3), "agent_id": "office", "entity_id": "light.desk"}]
    )
    assert [(c.cause, c.ref) for c in changes] == [("agent", "office"), ("physical", None)]


def follow_rows(days=6, extra_sensor=2) -> list[dict]:
    rows = []
    for day in range(days):
        rows += [entry(at(day, 21, 0), "binary_sensor.desk_zone", "on"),
                 entry(at(day, 21, 0, 40), "light.desk", "on"),
                 entry(at(day, 23, 0), "binary_sensor.desk_zone", "off"),
                 entry(at(day, 23, 1), "light.desk", "off", context_user_id="owner")]  # fmt: skip
    for day in range(extra_sensor):  # the sensor also turns on without the lamp following
        rows += [
            entry(at(day, 10, 0), "binary_sensor.desk_zone", "on"),
            entry(at(day, 10, 30), "binary_sensor.desk_zone", "off"),
        ]
    return rows


def test_a_lamp_switched_on_by_hand_after_the_desk_sensor_is_a_habit():
    changes = changes_from_logbook(follow_rows(), None, [])
    habits = find_habits(changes, AREAS.get, days=14, min_days=5)
    on = next(h for h in habits if h["state"] == "on")
    assert (
        on["kind"] == "follow" and on["trigger"] == "binary_sensor.desk_zone" and on["trigger_state"] == "on"
    )
    assert on["days"] == 6 and on["count"] == 6 and on["recall"] == 1.0 and on["precision"] == 0.75
    assert on["median_delay_s"] == 40
    title, request = habit_request(on)
    assert "binary_sensor.desk_zone" in request and "6 times on 6 days" in request and "75%" in request


def test_too_few_days_or_a_sensor_in_another_room_is_no_habit():
    changes = changes_from_logbook(follow_rows(days=4), None, [])
    assert find_habits(changes, AREAS.get, days=14, min_days=5) == []
    elsewhere = {**AREAS, "binary_sensor.desk_zone": "hall"}
    habits = find_habits(changes_from_logbook(follow_rows(), None, []), elsewhere.get, days=7, min_days=5)
    assert all(h["kind"] != "follow" for h in habits)


def test_automated_changes_are_never_habits():
    rows = [{**r, **AUTO, "context_entity_id": "automation.owner"} if r["entity_id"] == "light.desk" else r
            for r in follow_rows()]  # fmt: skip
    assert find_habits(changes_from_logbook(rows, None, []), AREAS.get, days=14, min_days=5) == []


def test_the_same_time_of_day_is_a_habit():
    rows = [entry(at(day, 7, 5 + day * 3), "light.porch", "on", context_user_id="owner") for day in range(7)]
    rows.append(entry(at(3, 15, 0), "light.porch", "on"))
    habits = find_habits(changes_from_logbook(rows, None, []), AREAS.get, days=7, min_days=5)
    assert len(habits) == 1 and habits[0]["kind"] == "time" and habits[0]["days"] == 7
    assert habits[0]["at"].startswith("07:") and habits[0]["weekdays"] == list(range(7))
    assert "about 07:" in habit_request(habits[0])[1]


OURS = {
    "automation.agent_house_hall": {"ha_id": "agent_house_hall", "version": 2, "since": 0, "title": "Hall"}
}


def test_undos_count_only_quick_manual_reversals_of_ours():
    rows = []
    for day in range(4):
        rows.append(
            entry(at(day, 22, 0), "light.hall", "on", **AUTO, context_entity_id="automation.agent_house_hall")
        )
        if day < 3:  # the owner switches it back off within 2 minutes
            rows.append(entry(at(day, 22, 2), "light.hall", "off", context_user_id="owner"))
    rows.append(
        entry(at(5, 22, 0), "light.hall", "on", **AUTO, context_entity_id="automation.agent_house_hall")
    )
    rows.append(entry(at(5, 22, 30), "light.hall", "off", context_user_id="owner"))  # too late: no undo
    rows.append(entry(at(6, 22, 0), "light.hall", "on", **AUTO, context_entity_id="automation.owner_own"))
    rows.append(entry(at(6, 22, 1), "light.hall", "off"))  # the owner's automation: not ours
    [item] = find_undos(changes_from_logbook(rows, None, []), OURS)
    assert (item["acted"], item["undone"], item["rate"]) == (5, 3, 0.6)
    assert item["flagged"] and item["suggest_rollback"] and len(item["examples"]) == 3
    old = {k: {**v, "since": at(5, 0, 0)} for k, v in OURS.items()}
    [item] = find_undos(changes_from_logbook(rows, None, []), old)
    assert item["acted"] == 1 and not item["flagged"], "only changes since the current version count"


def test_two_automations_fighting_over_one_entity_is_one_conflict():
    rows = []
    for day in range(3):
        rows.append(
            entry(at(day, 20, 0), "light.hall", "on", **AUTO, context_entity_id="automation.agent_house_hall")
        )
        rows.append(
            entry(at(day, 20, 0, 30), "light.hall", "off", **AUTO, context_entity_id="automation.owner_own")
        )
    rows.append(entry(at(4, 20, 0), "light.hall", "on", **AUTO, context_entity_id="automation.x"))
    rows.append(entry(at(4, 20, 0, 5), "light.hall", "off", **AUTO, context_entity_id="automation.y"))
    [conflict] = find_conflicts(changes_from_logbook(rows, None, []), OURS)
    assert conflict["entity_id"] == "light.hall" and conflict["count"] == 3
    assert conflict["sides"] == ["automation:automation.agent_house_hall", "automation:automation.owner_own"]


def test_change_manual_flag():
    assert Change(0, "light.x", "on", "physical").manual and not Change(0, "light.x", "on", "agent").manual
