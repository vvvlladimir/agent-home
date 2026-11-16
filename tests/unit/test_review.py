"""M5 daily review and the orchestrator's providers: the scan is deterministic, the summary is a
routine task on the cloud (D40: no local model), findings become one proposal each."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.config import RuntimeSettings
from app.core.insights import Insights
from app.core.lifecycle import Lifecycle
from app.core.review import Review
from tests.helpers import AutomationHA, ScriptedProvider, call, make_stack

GOOD = """alias: Lamp after motion
triggers: [{trigger: state, entity_id: binary_sensor.room_motion, to: "on"}]
actions: [{action: light.turn_on, target: {entity_id: light.room_lamp}}]
"""
BAD = GOOD.replace("light.turn_on", "lock.unlock").replace("light.room_lamp", "lock.room_door")


def submit(yaml: str) -> dict:
    return call("submit_automation", title="Lamp after motion", rationale="habit", expected_triggers="motion",
                yaml=yaml)  # fmt: skip


def stack(tmp_path, *script, provider="auto"):
    s = make_stack(tmp_path, ha=AutomationHA(), settings=RuntimeSettings(habit_days=14, habit_min_repeats=5))
    s.lifecycle = Lifecycle(s.store, s.feed, s.ha, s.safety, owner_of=s.council.ownership.owner_of)
    s.orch.lifecycle = s.lifecycle
    s.orch.settings.orchestrator_provider = provider
    s.cloud = s.orch.cloud = ScriptedProvider(list(script))
    s.openai = s.orch.openai = ScriptedProvider(name="openai")
    s.insights = Insights(s.store, s.feed, s.ha, s.lifecycle, s.settings)
    s.review = Review(s.store, s.feed, s.orch, s.insights, s.council, s.inbox, s.settings, s.lifecycle)
    return s


def habit_rows(days: int = 7) -> list[dict]:
    """Each evening: the room's motion, the lamp by hand 30 s later; off by hand when they leave."""
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    rows = []

    def row(moment, entity_id, state):
        rows.append({"when": moment.isoformat(), "entity_id": entity_id, "state": state})

    for back in range(1, days + 1):
        moment = now - timedelta(days=back)
        row(moment, "binary_sensor.room_motion", "on")
        row(moment + timedelta(seconds=30), "light.room_lamp", "on")
        row(moment + timedelta(hours=1), "binary_sensor.room_motion", "off")
        row(moment + timedelta(hours=1, seconds=40), "light.room_lamp", "off")
    return rows


async def test_the_review_scans_summarises_and_drafts_one_proposal_per_finding(tmp_path):
    s = stack(tmp_path, {"text": "Quiet day. One habit found."})
    s.ha.logbook_rows = habit_rows()
    result = await s.review.run()
    assert result["drafted"] and all(d["status"] == "draft" for d in result["drafted"])
    [finding] = s.insights.list("drafted")  # one proposal per entity per review: on or off of the lamp
    assert finding["id"].startswith("habit:light.room_lamp:") and finding["proposal_id"]
    proposal = s.lifecycle.get(finding["proposal_id"])
    assert proposal["origin_agent"] == "orchestrator" and "Situation: " in proposal["rationale"]
    assert "light.room_lamp" in proposal["yaml"] and len(s.cloud.requests) == 1, "built, not asked for"
    inbox = s.inbox.list()[0]
    assert inbox["agent_id"] == "orchestrator" and inbox["summary"].startswith("Daily review: Quiet day.")
    summary_request = s.cloud.requests[0]
    assert summary_request["tools"] == [] and "light.room_lamp" in summary_request["transcript"][0]["text"]
    # the same finding is never drafted twice
    s.cloud.steps = [{"text": "Again quiet."}]
    again = await s.review.run()
    assert again["drafted"] == [] and len(s.lifecycle.list()) == 1


async def test_the_review_works_without_a_model(tmp_path):
    s = stack(tmp_path, {"raise": "down"})
    s.openai.steps = [{"raise": "down too"}]
    result = await s.review.run()
    assert "Review of the 24 h" in result["summary"] and result["run_id"] is None
    keyless = stack(tmp_path)
    keyless.cloud.has_key = keyless.openai.has_key = False  # auto uses only the clouds with a key
    result = await keyless.review.run()
    assert "Review of the 24 h" in result["summary"] and keyless.cloud.requests == []


def test_the_review_is_due_once_a_day_and_not_hours_late(tmp_path):
    s = stack(tmp_path)
    day = datetime(2026, 11, 13, 4, 0).astimezone()
    assert not s.review.due(day)
    assert s.review.due(day.replace(hour=4, minute=45))
    assert not s.review.due(day.replace(hour=7, minute=0)), "missed by hours: wait for tomorrow"
    s.store.set("review_last_day", day.date().isoformat())
    assert not s.review.due(day.replace(hour=4, minute=45))


async def test_auto_falls_back_from_claude_to_openai_and_local_reads_as_auto(tmp_path):
    s = stack(tmp_path, {"raise": "overloaded"})
    s.openai.steps = [submit(GOOD)]
    proposal = await s.orch.draft("lamp on motion", task="habit")
    assert proposal["status"] == "draft" and len(s.cloud.requests) == 1 and len(s.openai.requests) == 1
    assert [(c["provider"], c["ok"], c["task"]) for c in s.orch.calls] == [
        ("anthropic", False, "habit"), ("openai", True, "habit")]  # fmt: skip
    s.orch.settings.orchestrator_provider = "local"  # an option from before 0.5.0
    assert s.orch.choice() == "auto" and s.orch.status()["provider"] == "auto"
    assert s.orch.choice("local") == "auto" and s.orch.choice("openai") == "openai"
    assert set(s.orch.status()["choices"]) == {"anthropic", "openai"}


async def test_a_pick_of_one_cloud_uses_only_that_one(tmp_path):
    s = stack(tmp_path, {"raise": "overloaded"}, provider="anthropic")
    s.openai.steps = [submit(GOOD), {"text": "Done."}]
    import pytest

    from app.core.orchestrator import OrchestratorError

    with pytest.raises(OrchestratorError) as err:
        await s.orch.draft("lamp on motion", task="habit")
    assert err.value.code == "unavailable" and s.openai.requests == []


# --- conflicts: one proposal per conflict, never two of ours fighting; one proposal per entity ----
OFF = GOOD.replace("Lamp after motion", "Lamp off").replace("light.turn_on", "light.turn_off")
AUTO = {"context_event_type": "automation_triggered", "context_domain": "automation"}


def fight_rows(first: str, second: str, times: int = 3) -> list[dict]:
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    rows = []
    for back in range(1, times + 1):
        moment = now - timedelta(days=back)
        rows.append({"when": moment.isoformat(), "entity_id": "light.room_lamp", "state": "on", **AUTO,
                     "context_entity_id": first})  # fmt: skip
        rows.append({"when": (moment + timedelta(seconds=30)).isoformat(), "entity_id": "light.room_lamp",
                     "state": "off", **AUTO, "context_entity_id": second})  # fmt: skip
    return rows


async def deploy(s, text: str) -> str:
    proposal = await s.lifecycle.create(text)
    return (await s.lifecycle.approve(proposal["id"]))["ha_id"]


async def test_a_conflict_between_two_of_ours_replaces_one_and_supersedes_the_other(tmp_path):
    s = stack(tmp_path, {"text": "One conflict."}, submit(GOOD), {"text": "Submitted."})
    on, off = await deploy(s, GOOD), await deploy(s, OFF)
    s.ha.logbook_rows = fight_rows(f"automation.{on}", f"automation.{off}")
    result = await s.review.run()
    [finding] = s.insights.list()
    assert finding["kind"] == "conflict" and result["drafted"][0]["status"] == "draft"
    proposal = s.lifecycle.get(finding["proposal_id"])
    mine, [other] = proposal["ha_id"], proposal["supersedes"]
    assert {mine, other} == {on, off}
    assert f"{other} will be switched off" in finding["request"]
    assert f"Supersedes {other}" in proposal["rationale"]
    s.ha.writes.clear()
    await s.lifecycle.approve(proposal["id"])
    assert [(w[0], w[1]) for w in s.ha.writes] == [("disable", other), ("write", mine)]
    enabled = {d["ha_id"]: d["enabled"] for d in s.lifecycle.deployed()}
    assert enabled == {mine: True, other: False}, "exactly one of ours acts on the lamp"


async def test_a_conflict_with_the_owners_automation_touches_nothing_of_theirs(tmp_path):
    s = stack(tmp_path, {"text": "One conflict."}, submit(GOOD), {"text": "Submitted."})
    on = await deploy(s, GOOD)
    s.ha.add("automation.owner_night", "on")
    s.ha.logbook_rows = fight_rows(f"automation.{on}", "automation.owner_night")
    await s.review.run()
    [finding] = s.insights.list()
    proposal = s.lifecycle.get(finding["proposal_id"])
    assert proposal["ha_id"] == on and proposal["supersedes"] == []
    assert "automation.owner_night is yours and stays untouched" in proposal["rationale"]
    assert "Migration page" in finding["request"]
    s.ha.writes.clear()
    await s.lifecycle.approve(proposal["id"])
    assert [(w[0], w[1]) for w in s.ha.writes] == [("write", on)]
    assert s.ha.states["automation.owner_night"]["state"] == "on"


async def test_one_proposal_per_entity_per_review_and_none_beside_an_open_one(tmp_path):
    s = stack(tmp_path, submit(GOOD), {"text": "Submitted."})
    lamp = {"entity_id": "light.room_lamp", "sides": ["automation:automation.a", "automation:automation.b"],
            "count": 2, "examples": []}  # fmt: skip
    s.insights._save("conflict:lamp", "conflict", "Conflict", "settle it", lamp, "room")
    s.insights._save("habit:lamp", "habit", "Lamp habit", "lamp on", {"entity_id": "light.room_lamp"}, "room")
    s.insights._save(
        "habit:ceiling", "habit", "Ceiling", "ceiling on", {"entity_id": "light.room_ceiling"}, "room"
    )
    await s.lifecycle.create(GOOD.replace("light.room_lamp", "light.room_ceiling"))  # open draft of ours
    drafted = await s.review.draft_new()
    assert [d["finding"] for d in drafted] == ["conflict:lamp"], "conflicts win; the rest waits"
    assert (
        s.insights.get("habit:lamp")["status"] == "new" and s.insights.get("habit:ceiling")["status"] == "new"
    )
    s.cloud.steps = []
    assert await s.review.draft_new() == [], "the lamp now has an open proposal of ours"


async def test_the_first_look_drafts_right_away_and_only_once(tmp_path):
    """D31 / QA-002: the owner does not wait for the daily review; the inbox says what was found."""
    s = stack(tmp_path)
    s.ha.logbook_rows = habit_rows()
    result = await s.review.first_look()
    assert result["drafted"] and all(d["status"] == "draft" for d in result["drafted"])
    assert len(result["drafted"]) == 1 <= result["habits"]["predictions"], (
        "on and off: one per entity per review"
    )
    assert result["habits"]["relevant"] >= 1
    [note] = [i for i in s.inbox.list() if i["summary"].startswith("First look")]
    assert "Proposed:" in note["summary"] and "light.room_lamp" in note["summary"]
    assert s.store.get("habits_first_look")["drafted"] == len(result["drafted"])
    s.ha.connected = True
    assert s.review.maybe_first_look() is False, "once"


async def test_an_ask_automation_passes_every_check_of_the_lifecycle(tmp_path):
    """D35: the ask (notify, wait for the button or the owner's own hand, clear, act) is a normal draft."""
    from app.core import predict
    from app.core.lifecycle import to_yaml

    s = stack(tmp_path)
    p = {"entity_id": "light.room_lamp", "state": "on", "mode": "ask", "name": "Lamp",
         "path": [{"f": ["is", "binary_sensor.room_motion", "on"], "k": 0, "left": False}]}  # fmt: skip
    proposal = await s.lifecycle.create(to_yaml(predict.automation(p, s.ha)), origin_agent="orchestrator")
    assert proposal["status"] == "draft", proposal["checks"]
    act = await s.lifecycle.create(
        to_yaml(predict.automation({**p, "mode": "act"}, s.ha)), origin_agent="orchestrator"
    )
    assert act["status"] == "draft", act["checks"]
