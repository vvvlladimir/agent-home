"""The single actuation path: policy → action log → HA, approvals with expiry, kill switch."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.actuator import ApprovalConflict
from tests.helpers import go_live, make_stack

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def stack(tmp_path):
    return make_stack(tmp_path)


async def request(stack, entity="light.room_ceiling", service="turn_on", data=None, **kw):
    return await stack.actuator.request("room", entity.split(".")[0], service, entity, data, **kw)


async def test_shadow_is_the_default_and_nothing_reaches_ha(stack):
    action = await request(stack)
    assert action["verdict"] == "shadow" and stack.ha.calls == []
    assert stack.actuator.list()[0]["id"] == action["id"]


async def test_live_needs_the_global_and_the_agent_switch(stack):
    stack.safety.set_mode("live")
    assert (await request(stack))["verdict"] == "shadow", "the agent itself is still in shadow mode"
    go_live(stack)
    action = await request(stack, data={"brightness_pct": 30})
    assert action["verdict"] == "executed" and action["tier"] == "T0"
    assert stack.ha.calls == [
        {
            "domain": "light",
            "service": "turn_on",
            "entity_id": "light.room_ceiling",
            "data": {"brightness_pct": 30},
        }
    ]


async def test_t3_outside_scope_and_unknown_service_never_reach_ha(stack):
    go_live(stack)
    stack.ha.add("light.other_room", "off", area="elsewhere")
    for entity, service in (
        ("lock.room_door", "unlock"),
        ("light.other_room", "turn_on"),
        ("light.room_ceiling", "flash_everything"),
        ("binary_sensor.room_motion", "turn_on"),
    ):
        assert (await request(stack, entity, service))["verdict"] == "denied"
    unknown = await stack.actuator.request("ghost", "light", "turn_on", "light.room_ceiling")
    assert unknown["verdict"] == "denied"
    assert stack.ha.calls == []
    assert len(stack.actuator.list(verdict="denied")) == 5, "refusals are logged too"


async def test_t1_acts_and_notifies(stack):
    go_live(stack)
    # a scene can touch any entity, so it needs approval unless the owner lowers it explicitly
    assert (await request(stack, "scene.room_movie"))["verdict"] == "pending"
    stack.safety.save_policy({"domain_tiers": {"scene": "T1"}})
    action = await request(stack, "scene.room_movie")
    assert action["verdict"] == "executed" and action["tier"] == "T1"
    item = stack.feed.recent(kind="action")[0]
    assert item["level"] == "warning", "T1 = act + notify"


async def test_t2_waits_for_approval_then_executes(stack):
    go_live(stack)
    action = await request(stack, "switch.room_heater")
    assert action["verdict"] == "pending" and action["expires"] > action["ts"] and stack.ha.calls == []
    assert stack.actuator.pending_count() == 1
    done = await stack.actuator.decide_approval(action["id"], True)
    assert done["verdict"] == "approved" and done["decided_by"] == "owner"
    assert [c["entity_id"] for c in stack.ha.calls] == ["switch.room_heater"]
    with pytest.raises(ApprovalConflict):
        await stack.actuator.decide_approval(action["id"], True)
    assert len(stack.ha.calls) == 1, "an approval is used once"


async def test_owner_denial_and_unknown_ids(stack):
    go_live(stack)
    action = await request(stack, "switch.room_heater")
    denied = await stack.actuator.decide_approval(action["id"], False)
    assert denied["verdict"] == "expired" and "owner" in denied["reason"] and stack.ha.calls == []
    with pytest.raises(ApprovalConflict):
        await stack.actuator.decide_approval("a_nope", True)


async def test_approval_expires(stack):
    go_live(stack)
    now = [1000.0]
    stack.actuator.clock = lambda: now[0]
    action = await request(stack, "switch.room_heater")
    assert action["expires"] == 1000.0 + 900
    now[0] += 901
    with pytest.raises(ApprovalConflict, match="expired"):
        await stack.actuator.decide_approval(action["id"], True)
    assert stack.ha.calls == [] and stack.actuator.list()[0]["verdict"] == "expired"
    assert stack.actuator.pending_count() == 0


async def test_approval_reruns_the_policy(stack):
    go_live(stack)
    first = await request(stack, "switch.room_heater")
    second = await request(stack, "climate.room", "set_temperature", {"temperature": 22})
    third = await request(stack, "switch.room_heater")
    stack.safety.set_kill(True, "test")
    assert (await stack.actuator.decide_approval(first["id"], True))["verdict"] == "denied"
    stack.safety.set_kill(False)
    stack.safety.set_mode("shadow")
    assert (await stack.actuator.decide_approval(second["id"], True))["verdict"] == "shadow"
    stack.safety.set_mode("live")
    stack.agents.delete("room")
    assert (await stack.actuator.decide_approval(third["id"], True))["verdict"] == "denied"
    assert stack.ha.calls == []


async def test_kill_switch_stops_a_call_that_was_already_decided(stack):
    """The kill switch is engaged between the decision and the send (e.g. from HA)."""
    go_live(stack)
    real_decide = stack.actuator._decide

    def decide_then_kill(*args, **kwargs):
        decision = real_decide(*args, **kwargs)
        stack.safety.set_kill(True, "engaged mid-flight")
        return decision

    stack.actuator._decide = decide_then_kill
    action = await request(stack)
    assert action["verdict"] == "denied" and "kill switch" in action["reason"] and stack.ha.calls == []


async def test_kill_switch_denies_everything(stack):
    go_live(stack)
    stack.safety.set_kill(True, "test")
    assert (await request(stack))["verdict"] == "denied"
    stack.safety.set_kill(False)
    assert (await request(stack))["verdict"] == "executed"


async def test_ha_errors_are_logged_as_failed(stack):
    go_live(stack)
    stack.ha.fail = "service_not_found: nope"
    action = await request(stack)
    assert action["verdict"] == "failed" and "nope" in action["reason"]


async def test_rate_limit_per_entity(stack):
    go_live(stack)
    verdicts = [(await request(stack))["verdict"] for _ in range(6)]
    assert verdicts == ["executed"] * 4 + ["denied"] * 2
    assert (await request(stack, "light.room_lamp"))["verdict"] == "executed", "other entities are unaffected"


async def test_protected_kill_switch_helper(tmp_path):
    stack = make_stack(tmp_path)
    stack.ha.add("input_boolean.agent_house_kill", "off", area="room")
    stack.actuator.protected = lambda: {"input_boolean.agent_house_kill"}
    go_live(stack)
    action = await request(stack, "input_boolean.agent_house_kill", "turn_off")
    assert action["verdict"] == "denied" and stack.ha.calls == []


async def test_broken_safety_state_fails_closed(stack):
    go_live(stack)
    stack.store.set("kill_switch", "corrupted")
    assert stack.safety.kill_engaged is True
    assert (await request(stack))["verdict"] == "denied"
    stack.store.set("kill_switch", {"engaged": False})
    stack.store.set("mode", "LIVE!")
    assert (await request(stack))["verdict"] == "shadow"
    stack.safety.policy_file.write_text("{ not yaml")
    stack.safety._policy = None
    stack.store.set("mode", "live")
    assert (await request(stack))["verdict"] == "denied", "an unreadable policy file allows nothing"


def test_only_the_actuator_calls_ha_call_service():
    """`Single actuation path`: no other module of the add-on may send a service call."""
    pattern = re.compile(r"\.call_service\(|[\"']call_service[\"']")
    offenders = []
    for file in (ROOT / "agent_house" / "app").rglob("*.py"):
        relative = file.relative_to(ROOT / "agent_house" / "app").as_posix()
        if relative in ("core/actuator.py", "ha/client.py"):
            continue
        for number, line in enumerate(file.read_text().splitlines(), 1):
            if (
                pattern.search(line)
                and "ha.call_service" in line
                or "_send(" in line
                and "call_service" in line
            ):
                offenders.append(f"{relative}:{number}")
    assert offenders == []
    actuator = (ROOT / "agent_house/app/core/actuator.py").read_text()
    assert actuator.count("self.ha.call_service(") == 1, "one send site, behind the kill switch re-check"
    client = (ROOT / "agent_house/app/ha/client.py").read_text()
    assert '"call_service"' not in client.split("READ_COMMANDS = frozenset(")[1].split(")")[0]
