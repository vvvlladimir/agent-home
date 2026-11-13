"""Automation lifecycle (safety layer): checks, drafts never reach HA, T3 never deployed, the
kill switch blocks deploys, only `agent_house_` automations are ever written."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.lifecycle import Lifecycle, LifecycleError, normalize
from app.core.safety import Safety
from app.ha.client import NotOurs, guard_ours
from app.store.db import Store
from app.store.feed import Feed
from tests.helpers import AutomationHA

GOOD = """
alias: Lamp on motion
triggers:
  - trigger: state
    entity_id: binary_sensor.room_motion
    to: "on"
actions:
  - action: light.turn_on
    target: {entity_id: light.room_lamp}
"""


@pytest.fixture
def lc(tmp_path):
    store = Store(":memory:")
    feed = Feed(store)
    ha = AutomationHA()
    ha.add("input_boolean.agent_house_kill", "off")
    life = Lifecycle(
        store, feed, ha, Safety(store, feed, tmp_path), protected=lambda: {"input_boolean.agent_house_kill"}
    )
    life.ha_stub = ha
    return life


def actions(*steps: str) -> str:
    body = "\n".join(f"  - {s}" for s in steps)
    return f"alias: x\ntriggers:\n  - trigger: state\n    entity_id: binary_sensor.room_motion\nactions:\n{body}\n"


def failed(proposal: dict) -> dict:
    return {c["name"]: c["detail"] for c in proposal["checks"] if not c["ok"]}


async def test_a_good_draft_passes_every_check_and_reaches_ha_only_on_approval(lc):
    proposal = await lc.create(GOOD, rationale="dark room", origin_agent="room")
    assert proposal["status"] == "draft", proposal["checks"]
    assert [c["name"] for c in proposal["checks"]] == ["yaml", "schema", "policy", "ha_validate"]
    assert proposal["ha_id"] == "agent_house_lamp_on_motion"
    assert proposal["diff"].startswith("--- (not deployed)")
    await lc.edit(proposal["id"], GOOD.replace("Lamp on motion", "Lamp on motion (dim)"))
    await lc.evidence(proposal["id"], 3)
    assert lc.ha_stub.writes == [], "drafts, edits and evidence never write to HA"

    deployed = await lc.approve(proposal["id"])
    assert deployed["status"] == "deployed"
    ((kind, ha_id, config),) = lc.ha_stub.writes
    assert (kind, ha_id) == ("write", "agent_house_lamp_on_motion")
    assert config["triggers"][0]["trigger"] == "state" and "id" not in config
    assert "[Agent House: proposal" in config["description"]
    listed = lc.deployed()
    assert listed[0]["current_version"] == 1 and listed[0]["present_in_ha"] and listed[0]["enabled"]
    assert lc.count_deployed() == 1 and lc.count_open() == 0
    with pytest.raises(LifecycleError, match="deployed"):
        await lc.approve(proposal["id"])


@pytest.mark.parametrize(
    ("step", "why"),
    [
        (
            "{action: lock.unlock, target: {entity_id: lock.room_door}}",
            "lock.unlock is not an allowed service",
        ),
        ("{action: lock.lock, target: {entity_id: lock.room_door}}", "T3"),
        ("{action: light.turn_on, target: {area_id: room}}", "area_id is not allowed"),
        ("{action: light.turn_on}", "must name its entity_id"),
        ("{action: light.turn_on, target: {entity_id: all}}", "literal entity id"),
        ("{action: light.turn_on, target: {entity_id: '{{ states.light | first }}'}}", "literal entity id"),
        ("{action: light.turn_on, target: {entity_id: switch.room_heater}}", "not a light entity"),
        ("{action: light.turn_on, target: {entity_id: light.nowhere}}", "unknown to Home Assistant"),
        (
            "{action: input_boolean.turn_off, target: {entity_id: input_boolean.agent_house_kill}}",
            "kill-switch",
        ),
        ("{action: automation.turn_off, target: {entity_id: automation.owners}}", "not an allowed service"),
        ("{action: script.turn_on, target: {entity_id: script.x}}", "not an allowed service"),
        ("{action: homeassistant.turn_off, target: {entity_id: light.room_lamp}}", "not an allowed service"),
        ("{action: '{{ svc }}', target: {entity_id: light.room_lamp}}", "literal domain.service"),
        ("{event: agent_house_kill_switch, event_data: {engaged: false}}", "firing events"),
        ("{scene: scene.nowhere}", "unknown"),
        (
            "{choose: [{conditions: [], sequence: [{action: lock.lock, target: {entity_id: lock.room_door}}]}]}",
            "T3",
        ),
        (
            "{if: [], then: [{delay: 1}], else: [{action: lock.lock, target: {entity_id: lock.room_door}}]}",
            "T3",
        ),
        ("{repeat: {count: 2, sequence: [{action: lock.lock, entity_id: lock.room_door}]}}", "T3"),
        ("{parallel: [{action: lock.lock, data: {entity_id: lock.room_door}}]}", "T3"),
        ("{shell: rm}", "unknown step"),
        (
            "{action: light.turn_on, target: \"{{ {'entity_id': 'lock.room_door'} }}\", data: {entity_id: light.room_lamp}}",
            "literal mapping",
        ),
        (
            "{action: light.turn_on, data_template: {entity_id: lock.room_door}, entity_id: light.room_lamp}",
            "data_template",
        ),
        ("{action: light.turn_on, target_template: x, entity_id: light.room_lamp}", "target_template"),
        ("{action: light.turn_on, data: '{{ x }}', entity_id: light.room_lamp}", "literal mapping"),
        ("{action: light.turn_on, target: {entity_id: light.room_lamp, area: x}}", "only name entity_id"),
    ],
)
async def test_the_policy_scan_blocks(lc, step, why):
    proposal = await lc.create(actions(step))
    assert proposal["status"] == "blocked"
    assert why in failed(proposal)["policy"]
    with pytest.raises(LifecycleError):
        await lc.approve(proposal["id"])
    assert lc.ha_stub.writes == []


async def test_allowed_steps_and_notifications_pass(lc):
    ok = actions(
        "{action: notify.mobile_app_phone, data: {message: hi}}",
        "{delay: '00:00:05'}",
        "{service: light.turn_off, entity_id: light.room_ceiling}",
        "{if: [{condition: state, entity_id: light.room_lamp, state: 'on'}], then: [{action: light.turn_off, target: {entity_id: light.room_lamp}}]}",
    )
    proposal = await lc.create(ok)
    assert proposal["status"] == "draft", failed(proposal)


async def test_t2_entities_are_named_for_the_owner_and_the_t3_whitelist_is_honoured(lc):
    proposal = await lc.create(actions("{action: switch.turn_on, target: {entity_id: switch.room_heater}}"))
    assert proposal["status"] == "draft"
    assert "T2, approved with this automation: switch.room_heater" in proposal["checks"][2]["detail"]


async def test_schema_and_yaml_errors(lc):
    assert failed(await lc.create("alias: [unclosed"))["yaml"]
    assert failed(await lc.create("- a\n- b"))["yaml"]
    bad = failed(await lc.create("alias: x\nid: '123'\ntriggers: []\nactions: []"))
    assert "unknown key 'id'" in bad["schema"] and "triggers must be" in bad["schema"]
    webhook = actions("{delay: 1}").replace("trigger: state", "trigger: webhook")
    assert "webhook" in failed(await lc.create(webhook))["policy"]
    ha = failed(await lc.create(actions("{delay: 1}").replace("trigger: state", "trigger: nonsense")))
    assert "Invalid trigger" in ha["ha_validate"]


def test_singular_keys_and_platform_are_normalised():
    config = normalize(
        {"alias": "a", "trigger": {"platform": "state"}, "action": [{"service": "light.turn_on"}]}
    )
    assert config == {
        "alias": "a",
        "triggers": [{"trigger": "state"}],
        "actions": [{"action": "light.turn_on"}],
    }


async def test_the_kill_switch_blocks_deploys_rollbacks_and_enabling(lc):
    proposal = await lc.create(GOOD)
    lc.safety.set_kill(True, "test")
    with pytest.raises(LifecycleError) as err:
        await lc.approve(proposal["id"])
    assert err.value.code == "kill_switch" and lc.ha_stub.writes == []
    assert lc.get(proposal["id"])["status"] == "draft"
    lc.safety.set_kill(False)
    await lc.approve(proposal["id"])
    second = await lc.create(
        GOOD.replace("light.room_lamp", "light.room_ceiling"), ha_id="agent_house_lamp_on_motion"
    )
    await lc.approve(second["id"])
    lc.safety.set_kill(True, "test")
    for attempt in (
        lc.rollback("agent_house_lamp_on_motion", 1),
        lc.set_enabled("agent_house_lamp_on_motion", True),
    ):
        with pytest.raises(LifecycleError, match="kill switch"):
            await attempt
    await lc.set_enabled("agent_house_lamp_on_motion", False)  # making things safer stays possible
    await lc.remove("agent_house_lamp_on_motion")
    assert [w[0] for w in lc.ha_stub.writes] == ["write", "write", "disable", "delete"]


async def test_versions_diff_and_rollback(lc):
    first = await lc.approve((await lc.create(GOOD))["id"])
    change = await lc.create(GOOD.replace("light.room_lamp", "light.room_ceiling"), ha_id=first["ha_id"])
    assert (
        "-    entity_id: light.room_lamp" in change["diff"]
        and "agent_house_lamp_on_motion v1" in change["diff"]
    )
    stale = await lc.create(GOOD.replace("Lamp", "Old lamp"), ha_id=first["ha_id"])
    await lc.approve(change["id"])
    assert lc.get(first["id"])["status"] == "superseded" and lc.get(stale["id"])["status"] == "superseded"
    rolled = await lc.rollback(first["ha_id"], 1)
    assert rolled["current_version"] == 3 and rolled["versions"][0]["note"] == "rollback to v1"
    assert lc.get(change["id"])["status"] == "rolled_back"
    assert lc.ha_stub.automations[first["ha_id"]]["actions"][0]["target"]["entity_id"] == "light.room_lamp"
    with pytest.raises(LifecycleError, match="already deployed"):
        await lc.rollback(first["ha_id"], 3)


async def test_a_rollback_is_checked_against_todays_policy(lc):
    first = await lc.approve(
        (await lc.create(actions("{action: switch.turn_on, entity_id: switch.room_heater}")))["id"]
    )
    await lc.approve((await lc.create(GOOD, ha_id=first["ha_id"]))["id"])
    lc.safety.save_policy({**lc.safety.policy, "entity_tiers": {"switch.room_heater": "T3"}})
    with pytest.raises(LifecycleError, match="no longer passes"):
        await lc.rollback(first["ha_id"], 1)


async def test_approval_rechecks_and_ha_errors_leave_a_draft(lc):
    proposal = await lc.create(GOOD)
    lc.ha_stub.fail = "boom"
    with pytest.raises(LifecycleError) as err:
        await lc.approve(proposal["id"])
    assert err.value.code == "ha_error" and lc.get(proposal["id"])["status"] == "draft"
    lc.ha_stub.fail = None
    del lc.ha_stub.states["light.room_lamp"]
    with pytest.raises(LifecycleError, match="check failed"):
        await lc.approve(proposal["id"])
    assert lc.get(proposal["id"])["status"] == "blocked" and lc.ha_stub.writes == []


async def test_never_the_owners_automations(lc):
    for bad in ("1700000000001", "agent_house_", "agent_house_x/../1", "my_agent_house_x", None):
        with pytest.raises(NotOurs):
            guard_ours(bad)
    with pytest.raises(NotOurs):
        await lc.create(GOOD, ha_id="1700000000001")
    with pytest.raises(LifecycleError, match="not one of our"):
        await lc.create(GOOD, ha_id="agent_house_not_deployed")
    lc.ha_stub.automations["agent_house_lamp_on_motion"] = {"alias": "made by hand"}
    proposal = await lc.create(GOOD)
    with pytest.raises(LifecycleError, match="not deployed by us"):
        await lc.approve(proposal["id"])
    for call in (
        lc.rollback("1700000000001", 1),
        lc.remove("1700000000001"),
        lc.set_enabled("1700000000001", False),
    ):
        with pytest.raises(LifecycleError, match="not one of our"):
            await call
    assert lc.ha_stub.writes == []


async def test_a_double_approve_deploys_once(lc):
    import asyncio

    proposal = await lc.create(GOOD)
    results = await asyncio.gather(
        lc.approve(proposal["id"]), lc.approve(proposal["id"]), return_exceptions=True
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert len(lc.ha_stub.writes) == 1


async def test_evidence_simulates_state_numeric_and_time_triggers(lc):
    now = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)

    def at(hours: float) -> str:
        return (now - timedelta(hours=hours)).isoformat()

    lc.ha_stub.history_rows = {
        "binary_sensor.room_motion": [
            {"state": "off", "last_changed": at(48)}, {"state": "on", "last_changed": at(30)},
            {"state": "off", "last_changed": at(29)}, {"state": "on", "last_changed": at(2)},
        ],
        "sensor.room_illuminance": [
            {"state": "40", "last_changed": at(48)}, {"state": "5", "last_changed": at(20)},
            {"state": "3", "last_changed": at(19)}, {"state": "50", "last_changed": at(10)},
            {"state": "unavailable", "last_changed": at(9)}, {"state": "4", "last_changed": at(8)},
        ],
    }  # fmt: skip
    from app.core.evidence import simulate

    config = normalize({
        "alias": "x",
        "trigger": [
            {"platform": "state", "entity_id": "binary_sensor.room_motion", "to": "on"},
            {"trigger": "numeric_state", "entity_id": "sensor.room_illuminance", "below": 10},
            {"trigger": "time", "at": "07:30:00"},
            {"trigger": "sun", "event": "sunset"},
        ],
        "action": [{"delay": 1}],
    })  # fmt: skip
    result = await simulate(config, lc.ha_stub, days=2, now=now)
    labels = [s["trigger"] for s in result["samples"]]
    assert labels.count("binary_sensor.room_motion: off → on") == 2
    assert sum("sensor.room_illuminance" in label for label in labels) == 2, "entering the range counts"
    assert labels.count("time 07:30") in (2, 3)
    assert result["supported"] is False and "sun" in result["unsupported"][0]
    assert result["would_fire"] == len(result["samples"])
