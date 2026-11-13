"""The policy decision function. These invariants are the safety layer: they must never regress."""

from __future__ import annotations

import copy
import itertools

import pytest

from app.core.policy import DEFAULT_POLICY, decide, effective_policy

SCOPE = frozenset(
    {"light.a", "scene.a", "climate.a", "switch.a", "lock.a", "sensor.a", "cover.garage_door", "foo.a"}
)
LIVE_AGENT = {"enabled": True, "mode": "live", "tier_overrides": {}, "limits": {"max_actions_per_minute": 6}}
SERVICE = {
    "light": "turn_on", "scene": "turn_on", "climate": "set_temperature", "switch": "turn_on",
    "lock": "unlock", "sensor": "turn_on", "cover": "open_cover", "foo": "turn_on",
}  # fmt: skip


_DEFAULT = object()


def ask(entity_id="light.a", service=_DEFAULT, **changes):
    domain = entity_id.split(".")[0] if isinstance(entity_id, str) and "." in entity_id else "light"
    args = {
        "domain": domain,
        "service": SERVICE.get(domain, "turn_on") if service is _DEFAULT else service,
        "entity_id": entity_id,
        "data": {},
        "agent": LIVE_AGENT,
        "scope": SCOPE,
        "kill_engaged": False,
        "global_mode": "live",
    }
    args.update(changes)
    return decide(**args)


def test_the_happy_paths_per_tier():
    assert ask("light.a").verdict == "allow" and ask("light.a").tier == "T0"
    assert ask("scene.a").verdict == "pending" and ask("scene.a").tier == "T2"  # a scene can touch anything
    assert ask("climate.a").verdict == "pending" and ask("climate.a").tier == "T2"
    assert ask("climate.a", approved=True).verdict == "allow"
    assert ask("lock.a").verdict == "denied" and ask("lock.a").tier == "T3"


def test_order_kill_switch_beats_everything():
    for entity, approved, mode in itertools.product(SCOPE, (True, False), ("live", "shadow")):
        decision = ask(entity, kill_engaged=True, approved=approved, global_mode=mode)
        assert decision.verdict == "denied" and "kill switch" in decision.reason
    for broken in (None, "false", 0, "off"):  # only a real False releases
        assert ask(kill_engaged=broken).verdict == "denied"


def test_t3_is_never_anything_but_denied():
    """Every T3 entity × every lever a caller could pull."""
    for entity in ("lock.a", "sensor.a", "cover.garage_door", "foo.a"):
        for approved, shadow, mode, agent_mode in itertools.product(
            (True, False), (True, False), ("live", "shadow"), ("live", "shadow")
        ):
            for service in ("turn_on", "unlock", "open_cover", SERVICE[entity.split(".")[0]]):
                decision = ask(
                    entity,
                    service,
                    approved=approved,
                    force_shadow=shadow,
                    global_mode=mode,
                    agent={**LIVE_AGENT, "mode": agent_mode, "tier_overrides": {entity: "T0"}},
                )
                assert decision.verdict == "denied", (entity, service, approved, shadow, mode)


def test_t3_whitelist_lifts_to_approval_never_further():
    policy = copy.deepcopy(DEFAULT_POLICY)
    policy["t3_whitelist"] = ["lock.a"]
    policy["allowed_services"]["lock"] = ["lock"]
    assert ask("lock.a", "lock", policy=policy).verdict == "pending"
    assert ask("lock.a", "lock", policy=policy, approved=True).verdict == "allow"
    assert ask("lock.a", "unlock", policy=policy, approved=True).verdict == "denied"  # service not listed


def test_entity_allowlist():
    assert ask("light.elsewhere").verdict == "denied"
    assert "allowlist" in ask("light.elsewhere").reason
    assert ask(scope=None).verdict == "denied"
    assert ask(scope=["light.a"]).verdict == "denied", "the scope must be a set the caller built on purpose"
    assert ask(protected={"light.a"}).verdict == "denied"


def test_service_allowlist_and_domain_must_match_the_entity():
    assert ask("light.a", "set_color_wheel").verdict == "denied"
    assert ask("light.a", domain="homeassistant", service="turn_on").verdict == "denied"
    assert ask("lock.a", domain="light", service="turn_on").verdict == "denied"
    assert ask("light.a", policy={**DEFAULT_POLICY, "allowed_services": None}).verdict == "denied"
    assert (
        ask("light.a", policy={**DEFAULT_POLICY, "allowed_services": {"light": "turn_on"}}).verdict
        == "denied"
    )


@pytest.mark.parametrize("key", ["entity_id", "area_id", "device_id", "floor_id", "label_id", "target"])
def test_service_data_cannot_smuggle_another_target(key):
    assert ask(data={key: "lock.a"}).verdict == "denied"


@pytest.mark.parametrize(
    "changes",
    [
        {"entity_id": None},
        {"entity_id": "light.a,lock.a"},
        {"entity_id": ["light.a"]},
        {"entity_id": "all"},
        {"service": None},
        {"service": "turn_on;x"},
        {"domain": 5},
        {"data": "brightness=3"},
        {"data": None},
        {"agent": None},
        {"agent": {**LIVE_AGENT, "enabled": False}},
        {"agent": {"mode": "live"}},
        {"policy": "nonsense"},
        {"policy": {}},
        {"recent": {"entity": "many"}},
    ],
)
def test_anything_malformed_is_denied(changes):
    assert ask(**changes).verdict == "denied"


def test_rate_limits_per_entity_agent_and_global():
    assert ask(recent={"entity": 3, "agent": 5, "global": 29}).verdict == "allow"
    assert "entity" in ask(recent={"entity": 4}).reason and ask(recent={"entity": 4}).verdict == "denied"
    assert ask(recent={"agent": 6}).verdict == "denied"
    assert ask(recent={"global": 30}).verdict == "denied"
    silenced = {**LIVE_AGENT, "limits": {"max_actions_per_minute": 0}}
    assert ask(agent=silenced).verdict == "denied"
    broken = {**DEFAULT_POLICY, "rate_limits": {"per_entity_per_minute": "lots"}}
    assert ask(policy=broken).verdict == "denied", "a malformed limit allows nothing"
    assert ask("climate.a", approved=True, recent={"entity": 4}).verdict == "denied"


def test_mode_live_needs_both_switches_and_shadow_wins_over_approval():
    assert ask(global_mode="shadow").verdict == "shadow"
    assert ask(agent={**LIVE_AGENT, "mode": "shadow"}).verdict == "shadow"
    assert ask(global_mode="LIVE").verdict == "shadow"
    assert ask(force_shadow=True).verdict == "shadow"
    assert ask("climate.a", global_mode="shadow").verdict == "shadow", "no approval requests in shadow mode"
    assert ask("climate.a", approved=True, force_shadow=True).verdict == "shadow"
    assert ask("lock.a", global_mode="shadow").verdict == "denied", "shadow never hides a refusal"


def test_agent_overrides_only_tighten():
    assert ask(agent={**LIVE_AGENT, "tier_overrides": {"light.a": "T2"}}).verdict == "pending"
    assert ask(agent={**LIVE_AGENT, "tier_overrides": {"light.*": "T3"}}).verdict == "denied"
    assert ask("climate.a", agent={**LIVE_AGENT, "tier_overrides": {"climate.a": "T0"}}).verdict == "pending"


def test_effective_policy_merges_the_owner_file_over_the_defaults():
    merged = effective_policy({"domain_tiers": {"light": "T2"}, "rate_limits": {"global_per_minute": 5}})
    assert merged["domain_tiers"] == {"light": "T2"}, "a present key replaces the default as a whole"
    assert merged["rate_limits"] == {"per_entity_per_minute": 4, "global_per_minute": 5}
    assert merged["allowed_services"] == DEFAULT_POLICY["allowed_services"]
    assert effective_policy("garbage") == DEFAULT_POLICY
    assert ask("fan.a", scope=frozenset({"fan.a"}), policy=merged).verdict == "denied", (
        "unlisted domain is T3"
    )
