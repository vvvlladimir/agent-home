"""The non-negotiables in one place (PLAN M4 safety tests, ported to the actors of D40: the
orchestrator / house manager and the learner): T3 is never actuated or deployed, drafts never reach
Home Assistant, the kill switch halts actions, drafts, model calls and deploys."""

from __future__ import annotations

import pytest

from app.core.actuator import ApprovalConflict
from app.core.lifecycle import Lifecycle, LifecycleError
from app.core.orchestrator import OrchestratorError
from app.core.policy import DEFAULT_POLICY
from tests.helpers import AutomationHA, FakeLearner, ScriptedProvider, call, go_live, make_stack

GOOD = """alias: a
triggers: [{trigger: state, entity_id: binary_sensor.room_motion, to: "on"}]
actions: [{action: light.turn_on, target: {entity_id: light.room_lamp}}]
"""


def stack(tmp_path, *script, learner=None):
    s = make_stack(tmp_path, ha=AutomationHA(), learner=learner)
    s.lifecycle = Lifecycle(s.store, s.feed, s.ha, s.safety, owner_of=s.council.ownership.owner_of)
    s.orch.lifecycle = s.lifecycle
    s.cloud = s.orch.cloud = ScriptedProvider(list(script))
    s.orch.openai = ScriptedProvider(name="openai")
    s.orch.settings.anthropic_api_key = "sk-ant-test"
    return s


@pytest.mark.parametrize("actor", ["orchestrator", "learner"])
@pytest.mark.parametrize("service", ["lock", "unlock", "turn_on", "open"])
async def test_t3_is_never_actuated_even_live_and_approved(tmp_path, actor, service):
    s = stack(tmp_path, learner=FakeLearner(["lock.room_door", "light.room_lamp"]))
    go_live(s, actor)
    action = await s.actuator.request(actor, "lock", service, "lock.room_door")
    assert action["verdict"] == "denied"
    s.safety.save_policy({"entity_tiers": {"lock.room_door": "T0"}})  # a T3 domain stays T3
    assert (await s.actuator.request(actor, "lock", service, "lock.room_door"))["verdict"] == "denied"
    assert s.ha.calls == []
    assert all(a["verdict"] == "denied" for a in s.actuator.list())
    for action in s.actuator.list():
        with pytest.raises(ApprovalConflict):
            await s.actuator.decide_approval(action["id"], True)
    assert DEFAULT_POLICY["domain_tiers"]["lock"] == "T3"


async def test_shadow_is_the_default_for_every_actor(tmp_path):
    s = stack(tmp_path, learner=FakeLearner(["light.room_lamp"]))
    for actor in ("orchestrator", "learner"):
        assert (await s.actuator.request(actor, "light", "turn_on", "light.room_lamp"))["verdict"] == "shadow"
    s.safety.set_mode("live")  # the global switch alone moves nobody
    for actor in ("orchestrator", "learner"):
        assert (await s.actuator.request(actor, "light", "turn_on", "light.room_lamp"))["verdict"] == "shadow"
    assert s.ha.calls == []


async def test_unknown_actors_and_old_room_agent_ids_are_denied(tmp_path):
    s = stack(tmp_path, learner=FakeLearner(["light.room_lamp"]))
    go_live(s, "orchestrator", "learner")
    for actor in ("room", "bedroom", "owner", "", "house"):
        assert (await s.actuator.request(actor, "light", "turn_on", "light.room_lamp"))["verdict"] == "denied"
    assert s.ha.calls == []


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
    proposal = await s.orch.draft("lamp on at motion")
    await s.lifecycle.edit(proposal["id"], GOOD.replace("alias: a", "alias: b"))
    await s.lifecycle.evidence(proposal["id"])
    s.lifecycle.reject(proposal["id"])
    assert s.ha.writes == [] and s.ha.calls == []


async def test_the_kill_switch_halts_everything(tmp_path):
    s = stack(tmp_path, learner=FakeLearner(["light.room_lamp"]))
    go_live(s, "orchestrator", "learner")
    proposal = await s.lifecycle.create(GOOD)
    s.safety.set_kill(True, "test")
    for actor in ("orchestrator", "learner"):
        action = await s.actuator.request(actor, "light", "turn_on", "light.room_lamp")
        assert action["verdict"] == "denied"
    with pytest.raises(LifecycleError, match="kill switch"):
        await s.lifecycle.approve(proposal["id"])
    with pytest.raises(OrchestratorError, match="kill switch"):
        await s.orch.draft("lamp on")
    with pytest.raises(OrchestratorError, match="kill switch"):
        await s.orch.chat("is the lamp on?")
    assert s.manager.request("manual", force=True)["status"] == "refused"
    assert s.ha.calls == [] and s.ha.writes == [] and s.cloud.requests == []


async def test_a_manager_run_started_before_the_kill_switch_cannot_actuate(tmp_path):
    s = stack(tmp_path, call("call_service", domain="light", service="turn_on", entity_id="light.room_lamp",
                             data={}), {"text": "done"})  # fmt: skip
    go_live(s)
    original = s.cloud.turn

    async def turn_then_kill(*args, **kwargs):
        result = await original(*args, **kwargs)
        s.safety.set_kill(True, "pressed mid-run")
        return result

    s.cloud.turn = turn_then_kill
    await s.manager.run(["manual: test"])
    assert s.ha.calls == []
    assert [a["verdict"] for a in s.actuator.list()] == ["denied"]


async def test_the_learner_acts_only_on_its_live_targets_and_never_on_helpers(tmp_path):
    from app.core.council import MODE_ENTITY

    s = stack(tmp_path, learner=FakeLearner(["light.room_lamp"]))
    s.ha.add(MODE_ENTITY, "home")
    go_live(s, "learner")
    assert (await s.actuator.request("learner", "light", "turn_on", "light.room_lamp"))["verdict"] == "executed"
    for entity, domain, service in (("light.room_ceiling", "light", "turn_on"),
                                    (MODE_ENTITY, "input_select", "select_option")):  # fmt: skip
        assert (await s.actuator.request("learner", domain, service, entity))["verdict"] == "denied"
    assert [c["entity_id"] for c in s.ha.calls] == ["light.room_lamp"]


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


def test_only_the_add_on_option_lifts_a_t3_never_the_policy_file(tmp_path):
    """QA-004: a t3_whitelist written into policy.yaml (the API's file) is ignored and not kept;
    the add-on option lifts to T2, never lower."""
    from app.core.policy import resolve_tier
    from app.core.safety import Safety
    from app.store.db import Store
    from app.store.feed import Feed

    store = Store(tmp_path / "db.sqlite")
    feed = Feed(store)
    plain = Safety(store, feed, tmp_path)
    saved = plain.save_policy({**plain.policy, "t3_whitelist": ["lock.front_door"]})
    assert saved["t3_whitelist"] == [] and "t3_whitelist" not in (tmp_path / "policy.yaml").read_text()
    assert resolve_tier("lock.front_door", plain.policy) == "T3"
    owner = Safety(store, feed, tmp_path, ["lock.front_door"])
    assert resolve_tier("lock.front_door", owner.policy) == "T2"
