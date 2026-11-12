"""Tier resolution is the root of the safety layer: these invariants must never regress."""

from __future__ import annotations

import copy
import itertools

import pytest

from app.core.policy import DEFAULT_POLICY, TIERS, resolve_tier, schema_defaults


def policy(**changes) -> dict:
    result = copy.deepcopy(DEFAULT_POLICY)
    result.update(changes)
    return result


def test_defaults_match_the_contract_schema():
    assert schema_defaults() == DEFAULT_POLICY


@pytest.mark.parametrize(
    ("entity", "tier"),
    [
        ("light.kitchen", "T0"),
        ("media_player.tv", "T0"),
        ("fan.bedroom", "T0"),
        ("scene.movie", "T1"),
        ("climate.bedroom", "T2"),
        ("cover.blinds", "T2"),
        ("switch.space_heater", "T2"),
        ("lock.front_door", "T3"),
        ("alarm_control_panel.home", "T3"),
        ("valve.water_main", "T3"),
        ("cover.garage_door", "T3"),
        ("cover.big_garage", "T3"),
    ],
)
def test_default_tiers(entity, tier):
    assert resolve_tier(entity) == tier


@pytest.mark.parametrize("entity", ["shell_command.reboot", "script.x", "automation.x", "nonsense", ""])
def test_unknown_domains_are_denied_by_default(entity):
    assert resolve_tier(entity) == "T3"


def test_t3_domain_cannot_be_lowered_by_entity_tiers_alone():
    p = policy(entity_tiers={"lock.front_door": "T0", "lock.*": "T0"})
    assert resolve_tier("lock.front_door", p) == "T3"


def test_whitelist_lifts_t3_to_t2_and_never_lower():
    p = policy(t3_whitelist=["lock.front_door"], entity_tiers={"lock.front_door": "T0"})
    assert resolve_tier("lock.front_door", p) == "T2"
    assert resolve_tier("lock.back_door", p) == "T3"


def test_whitelist_is_exact_ids_only():
    p = policy(t3_whitelist=["lock.*"])
    assert resolve_tier("lock.front_door", p) == "T3"


def test_whitelisted_garage_cover_needs_approval():
    p = policy(t3_whitelist=["cover.garage_door"])
    assert resolve_tier("cover.garage_door", p) == "T2"


def test_exact_entity_tier_beats_glob_and_strictest_glob_wins():
    p = policy(entity_tiers={"light.*": "T2", "light.k*": "T1", "light.kitchen": "T0"})
    assert resolve_tier("light.kitchen", p) == "T0"
    assert resolve_tier("light.kettle", p) == "T2"


def test_agent_overrides_only_tighten():
    assert resolve_tier("light.kitchen", agent_overrides={"light.kitchen": "T2"}) == "T2"
    assert resolve_tier("climate.bedroom", agent_overrides={"climate.bedroom": "T0"}) == "T2"
    assert resolve_tier("lock.front_door", agent_overrides={"lock.front_door": "T0"}) == "T3"
    assert resolve_tier("light.kitchen", agent_overrides={"light.*": "T3"}) == "T3"


def test_no_combination_of_agent_overrides_ever_loosens():
    entities = ["light.a", "climate.a", "lock.a", "cover.garage_x", "foo.bar"]
    for entity, override in itertools.product(entities, TIERS):
        base = resolve_tier(entity)
        got = resolve_tier(entity, agent_overrides={entity: override})
        assert TIERS.index(got) >= TIERS.index(base)
