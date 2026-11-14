"""The orchestrator's drafting rules name the house-mode helper only when the house has it
(stories run 2026-11-14, bug 1: every draft was conditioned on a helper the house did not have)."""

from __future__ import annotations

from types import SimpleNamespace

from app.core.lifecycle import Lifecycle
from app.core.orchestrator import CHAT_SYSTEM, SYSTEM, Orchestrator
from tests.helpers import AutomationHA, make_stack


def test_the_house_mode_rule_follows_the_helper(tmp_path):
    s = make_stack(tmp_path, ha=AutomationHA(), llm=None)
    lifecycle = Lifecycle(s.store, s.feed, s.ha, s.safety)
    settings = SimpleNamespace(orchestrator_provider="local", anthropic_api_key="", anthropic_model="x",
                               anthropic_base_url="", daily_budget_eur=1.0, redact_cloud=True)  # fmt: skip
    orch = Orchestrator(settings, s.llm, s.store, s.feed, s.ha, s.safety, lifecycle)
    for template in (SYSTEM, CHAT_SYSTEM):
        text = orch._system(template)
        assert "agent_house_mode" not in text and "{" + "house_mode}" not in text
        assert "weekday:" in text
    s.ha.add("input_select.agent_house_mode", "home")
    assert "The house mode is input_select.agent_house_mode" in orch._system()
