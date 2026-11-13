"""The non-negotiables in one place (PLAN M4 safety tests): T3 is never actuated or deployed,
drafts never reach Home Assistant, the kill switch halts wakes, actions, drafts and deploys."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.actuator import ApprovalConflict
from app.core.lifecycle import Lifecycle, LifecycleError
from app.core.orchestrator import Orchestrator, OrchestratorError
from app.core.policy import DEFAULT_POLICY
from app.core.runtime import WakeRefused
from tests.helpers import AutomationHA, call, go_live, make_stack, run, steps

GOOD = """alias: a
triggers: [{trigger: state, entity_id: binary_sensor.room_motion, to: "on"}]
actions: [{action: light.turn_on, target: {entity_id: light.room_lamp}}]
"""


def stack(tmp_path, *script):
    s = make_stack(tmp_path, ha=AutomationHA(), llm=None)
    s.llm.steps = list(script)
    s.lifecycle = Lifecycle(s.store, s.feed, s.ha, s.safety, owner_of=s.council.ownership.owner_of)
    settings = SimpleNamespace(orchestrator_provider="local", anthropic_api_key="", anthropic_model="claude-haiku-5-5",
                               anthropic_base_url="", daily_budget_eur=1.0, redact_cloud=True)  # fmt: skip
    s.orch = Orchestrator(settings, s.llm, s.store, s.feed, s.ha, s.safety, s.lifecycle)
    s.rt.orchestrator = s.orch
    return s


@pytest.mark.parametrize("service", ["lock", "unlock", "turn_on", "open"])
async def test_t3_is_never_actuated_even_live_and_approved(tmp_path, service):
    s = stack(tmp_path, call("call_service", domain="lock", service=service, entity_id="lock.room_door"))
    go_live(s)
    await run(s)
    assert s.ha.calls == []
    assert all(a["verdict"] == "denied" for a in s.actuator.list())
    for action in s.actuator.list():
        with pytest.raises(ApprovalConflict):
            await s.actuator.decide_approval(action["id"], True)
    assert DEFAULT_POLICY["domain_tiers"]["lock"] == "T3"


async def test_t3_is_never_deployed(tmp_path):
    s = stack(tmp_path)
    proposal = await s.lifecycle.create(
        GOOD.replace("light.turn_on", "lock.lock").replace("light.room_lamp", "lock.room_door")
    )
    assert proposal["status"] == "blocked"
    with pytest.raises(LifecycleError):
        await s.lifecycle.approve(proposal["id"])
    assert s.ha.writes == []


async def test_drafts_never_reach_ha(tmp_path):
    submit = call("submit_automation", title="t", rationale="r", expected_triggers="e", yaml=GOOD)
    s = stack(tmp_path, submit, {"text": "ok"})
    s.ha.connected = True
    proposal = await s.orch.draft("lamp on at motion")
    await s.lifecycle.edit(proposal["id"], GOOD.replace("alias: a", "alias: b"))
    await s.lifecycle.evidence(proposal["id"])
    s.lifecycle.reject(proposal["id"])
    assert s.ha.writes == [] and s.ha.calls == []


async def test_the_kill_switch_halts_everything(tmp_path):
    s = stack(tmp_path, call("call_service", domain="light", service="turn_on", entity_id="light.room_lamp"))
    go_live(s)
    proposal = await s.lifecycle.create(GOOD)
    s.safety.set_kill(True, "test")
    with pytest.raises(WakeRefused):
        s.rt.wake("room", source="manual", reason="x")
    action = await s.actuator.request("room", "light", "turn_on", "light.room_lamp")
    assert action["verdict"] == "denied"
    with pytest.raises(LifecycleError, match="kill switch"):
        await s.lifecycle.approve(proposal["id"])
    with pytest.raises(OrchestratorError, match="kill switch"):
        await s.orch.draft("lamp on")
    assert s.orch.request_draft("room", None, "lamp on at motion", "room") is False
    assert s.ha.calls == [] and s.ha.writes == [] and s.llm.requests == []


async def test_a_run_started_before_the_kill_switch_cannot_actuate(tmp_path):
    s = stack(tmp_path)
    go_live(s)
    s.llm.steps = [call("call_service", domain="light", service="turn_on", entity_id="light.room_lamp")]
    s.ha.before_call = lambda: None
    original = s.llm.chat

    async def chat_then_kill(*args, **kwargs):
        result = await original(*args, **kwargs)
        s.safety.set_kill(True, "pressed mid-run")
        return result

    s.llm.chat = chat_then_kill
    result = await run(s)
    assert s.ha.calls == []
    assert [p["verdict"] for p in steps(result, "policy")] == ["denied"]


async def test_a_room_agent_cannot_start_unlimited_drafts(tmp_path):
    import time

    s = stack(tmp_path)
    for n in range(3):
        s.store.execute(
            "INSERT INTO runs (id, agent_id, status, wake_source, wake_reason, started) "
            "VALUES (?, 'orchestrator', 'ok', 'poke', 'room asks for an automation: x', ?)",
            (f"r_{n}", time.time()),
        )
    assert s.orch.request_draft("room", None, "lamp on at motion", "room") is False
    assert s.orch.request_draft("other", None, "lamp on at motion", "room") is True
    await s.orch.stop()


def test_the_orchestrators_first_look_shows_room_sensors_and_search_finds_the_rest(tmp_path):
    s = stack(tmp_path)
    for n in range(90):
        s.ha.add(f"light.filler_{n:02d}", "off", area="room")
    s.ha.add("binary_sensor.esphome_radar_livingroom_zone_1_occupancy", "on", area="room")
    s.ha.states["binary_sensor.esphome_radar_livingroom_zone_1_occupancy"]["attributes"] = {
        "device_class": "occupancy"
    }
    lines = s.orch.entity_lines("Когда я сижу за столом, лампа горит", None)
    assert any("zone_1_occupancy" in line for line in lines), "room sensors are not buried behind devices"
    assert sum(line.startswith("- light.") for line in lines) >= 25, "devices still get their share"
    assert lines[-1].startswith("(") and "find_entities" in lines[-1]
    found = s.orch.find_entities("zone occupancy", "room")
    assert "binary_sensor.esphome_radar_livingroom_zone_1_occupancy" in found and "occupancy" in found
    assert "No entity matches" in s.orch.find_entities("unicorn", None)


async def test_superseding_switches_off_only_ours_never_the_owners_and_never_under_the_kill_switch(tmp_path):
    from app.ha.client import NotOurs

    s = stack(tmp_path)
    ours = [(await s.lifecycle.approve((await s.lifecycle.create(GOOD.replace("alias: a", f"alias: {n}")))["id"]))
            ["ha_id"] for n in ("a", "b")]  # fmt: skip
    for owners in ("1700000000001", "automation.owner", "script.owner"):
        with pytest.raises(NotOurs):
            await s.lifecycle.create(GOOD, supersedes=[owners])
    proposal = await s.lifecycle.create(
        GOOD, ha_id=ours[0], supersedes=[ours[1]], origin_agent="orchestrator"
    )
    s.ha.writes.clear()
    s.safety.set_kill(True, "test")
    with pytest.raises(LifecycleError, match="kill switch"):
        await s.lifecycle.approve(proposal["id"])
    assert s.ha.writes == []
    s.safety.set_kill(False)
    await s.lifecycle.approve(proposal["id"])
    assert {w[1] for w in s.ha.writes if w[0] == "disable"} == {ours[1]}
    assert s.ha.automations["1700000000001"]["alias"] == "Owner's own"
