"""M5 history analysis: who caused a change, undone automations, automations that fight (habits:
test_habits.py).
Pure functions over logbook entries shaped like HA's (field names [UNVERIFIED-ON-HW])."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.core.insights import (
    Change,
    changes_from_logbook,
    classify,
    find_conflicts,
    find_undos,
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
