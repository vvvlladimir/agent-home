"""M5 daily review and the orchestrator's per-task providers: the scan is deterministic, the
summary is a routine (local) task, findings become one proposal each, the cloud is the expert."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.config import RuntimeSettings
from app.core.insights import Insights
from app.core.lifecycle import Lifecycle
from app.core.orchestrator import Orchestrator, Turn
from app.core.review import Review
from tests.helpers import AutomationHA, call, make_stack

GOOD = """alias: Lamp after motion
triggers: [{trigger: state, entity_id: binary_sensor.room_motion, to: "on"}]
actions: [{action: light.turn_on, target: {entity_id: light.room_lamp}}]
"""
BAD = GOOD.replace("light.turn_on", "lock.unlock").replace("light.room_lamp", "lock.room_door")


def submit(yaml: str) -> dict:
    return call("submit_automation", title="Lamp after motion", rationale="habit", expected_triggers="motion",
                yaml=yaml)  # fmt: skip


def stack(tmp_path, *script, provider="local"):
    s = make_stack(tmp_path, ha=AutomationHA(), settings=RuntimeSettings(habit_days=14, habit_min_repeats=5))
    s.llm.steps = list(script)
    s.lifecycle = Lifecycle(s.store, s.feed, s.ha, s.safety, owner_of=s.council.ownership.owner_of)
    settings = SimpleNamespace(orchestrator_provider=provider, anthropic_api_key="", anthropic_model="claude-haiku-5-5",
                               anthropic_base_url="", openai_api_key="", openai_model="gpt-6.1-sol",
                               openai_base_url="", daily_budget_eur=1.0, redact_cloud=True)  # fmt: skip
    s.orch = Orchestrator(settings, s.llm, s.store, s.feed, s.ha, s.safety, s.lifecycle, s.rt)
    s.orch.council = s.council
    s.insights = Insights(s.store, s.feed, s.ha, s.lifecycle, s.rt.settings)
    s.review = Review(s.store, s.feed, s.orch, s.insights, s.council, s.rt, s.rt.settings, s.lifecycle)
    return s


def habit_rows(days: int = 6) -> list[dict]:
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    rows = []
    for back in range(1, days + 1):
        moment = now - timedelta(days=back)
        rows.append({"when": moment.isoformat(), "entity_id": "binary_sensor.room_motion", "state": "on"})
        rows.append({"when": (moment + timedelta(seconds=30)).isoformat(), "entity_id": "light.room_lamp",
                     "state": "on"})  # fmt: skip
        rows.append({"when": (moment + timedelta(hours=1)).isoformat(), "entity_id": "binary_sensor.room_motion",
                     "state": "off"})  # fmt: skip
    return rows


async def test_the_review_scans_summarises_and_drafts_one_proposal_per_finding(tmp_path):
    s = stack(tmp_path, {"text": "Quiet day. One habit found."}, submit(GOOD), {"text": "Submitted."})
    s.ha.logbook_rows = habit_rows()
    result = await s.review.run()
    assert result["drafted"] and result["drafted"][0]["status"] == "draft"
    [finding] = s.insights.list()
    assert finding["kind"] == "habit" and finding["status"] == "drafted" and finding["proposal_id"]
    proposal = s.lifecycle.get(finding["proposal_id"])
    assert proposal["origin_agent"] == "orchestrator" and "Evidence: 6× on 6 days" in proposal["rationale"]
    inbox = s.rt.inbox()[0]
    assert inbox["agent_id"] == "orchestrator" and inbox["summary"].startswith("Daily review: Quiet day.")
    summary_request = s.llm.requests[0]
    assert summary_request["tools"] == [] and "light.room_lamp" in summary_request["messages"][1]["content"]
    # the same finding is never drafted twice
    s.llm.steps = [{"text": "Again quiet."}]
    again = await s.review.run()
    assert again["drafted"] == [] and len(s.lifecycle.list()) == 1


async def test_the_review_works_without_a_model(tmp_path):
    from app.core.llm import LLMError

    s = stack(tmp_path, {"raise": LLMError("down")}, {"raise": LLMError("down")})
    result = await s.review.run()
    assert "Review of the 24 h" in result["summary"] and result["run_id"] is None


def test_the_review_is_due_once_a_day_and_not_hours_late(tmp_path):
    s = stack(tmp_path)
    day = datetime(2026, 11, 13, 4, 0).astimezone()
    assert not s.review.due(day)
    assert s.review.due(day.replace(hour=4, minute=45))
    assert not s.review.due(day.replace(hour=7, minute=0)), "missed by hours: wait for tomorrow"
    s.store.set("review_last_day", day.date().isoformat())
    assert not s.review.due(day.replace(hour=4, minute=45))


class FakeCloud:
    name, has_key, available = "anthropic", True, True

    def __init__(self) -> None:
        self.turns = [Turn(calls=[{"id": "c1", "name": "submit_automation", "input": {
            "title": "Lamp", "rationale": "expert", "expected_triggers": "motion", "yaml": GOOD}}],
            provider="anthropic"), Turn(text="Done.", provider="anthropic")]  # fmt: skip
        self.calls = 0

    async def turn(self, system, transcript, tools, priority):
        self.calls += 1
        return self.turns.pop(0)


async def test_routine_stays_local_and_the_cloud_is_the_expert_for_blocked_drafts(tmp_path):
    s = stack(tmp_path, submit(BAD), {"text": "I cannot fix it."}, provider="auto")
    s.orch.cloud = FakeCloud()
    assert s.orch.providers("routine") == [s.orch.local]
    assert s.orch.providers("draft", expert=True) == [s.orch.cloud]
    proposal = await s.orch.draft("lamp on motion", task="habit")
    assert proposal["status"] == "draft" and s.orch.cloud.calls == 1
    blocked = [p for p in s.lifecycle.list() if p["status"] == "rejected"]
    assert len(blocked) == 1 and "expert" in blocked[0]["note"]


async def test_no_expert_without_auto(tmp_path):
    s = stack(tmp_path, submit(BAD), {"text": "I cannot fix it."}, provider="local")
    s.orch.cloud = FakeCloud()
    proposal = await s.orch.draft("lamp on motion", task="habit")
    assert proposal["status"] == "blocked" and s.orch.cloud.calls == 0
