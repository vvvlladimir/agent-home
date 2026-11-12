"""Policy engine, part 1: tier resolution (pure functions, no I/O).

Tiers: T0 free · T1 act + notify · T2 act only after human approval · T3 never.
Deny by default: a domain that is not listed is T3.

The decision function (kill switch, allowlists, shadow mode, rate limits, approvals) is added
in M3 on top of `resolve_tier`; changes to this file need a `reviewer` pass.
"""

from __future__ import annotations

import json
import re
from fnmatch import fnmatchcase
from pathlib import Path

TIERS = ("T0", "T1", "T2", "T3")
# Domains with nothing to actuate. They resolve to T3 like every unlisted domain; the GUI shows
# them as "read-only" so a wall of red T3 badges does not hide the entities that matter.
READ_ONLY_DOMAINS = frozenset(
    {"sensor", "binary_sensor", "person", "sun", "weather", "zone", "device_tracker", "event"}
)
_SCHEMA = Path(__file__).resolve().parents[3] / "contracts" / "schemas" / "policy.schema.json"

# Kept in sync with contracts/schemas/policy.schema.json by tests/unit/test_policy_tiers.py.
DEFAULT_POLICY: dict = {
    "domain_tiers": {
        "light": "T0",
        "media_player": "T0",
        "fan": "T0",
        "scene": "T1",
        "input_boolean": "T1",
        "humidifier": "T1",
        "vacuum": "T1",
        "climate": "T2",
        "cover": "T2",
        "switch": "T2",
        "water_heater": "T2",
        "input_number": "T2",
        "lock": "T3",
        "alarm_control_panel": "T3",
        "valve": "T3",
        "siren": "T3",
        "camera": "T3",
    },
    "entity_tiers": {"cover.*garage*": "T3"},
    "t3_whitelist": [],
}


_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


def _rank(tier: str) -> int:
    return TIERS.index(tier)


def _tier(value: object) -> str:
    """Anything that is not exactly T0..T3 (typo, wrong case, wrong type) fails closed to T3."""
    return value if isinstance(value, str) and value in TIERS else "T3"


def _matches(patterns: object, entity_id: str) -> tuple[str | None, list[str]]:
    """(exact entry, tiers of all matching globs), every value normalised by `_tier`."""
    if not isinstance(patterns, dict):
        return None, []
    exact = _tier(patterns[entity_id]) if entity_id in patterns else None
    globs = [
        _tier(tier)
        for pattern, tier in patterns.items()
        if pattern != entity_id and isinstance(pattern, str) and fnmatchcase(entity_id, pattern)
    ]
    return exact, globs


def resolve_tier(
    entity_id: str, policy: dict | None = None, agent_overrides: dict[str, str] | None = None
) -> str:
    """Tier of one entity under the global policy plus an agent's overrides.

    Invariants (tested in tests/unit/test_policy_tiers.py):
    - anything unexpected fails closed: unknown domain, malformed entity id, malformed tier value,
      malformed policy → T3;
    - if the domain, an exact `entity_tiers` entry or ANY matching glob says T3, the entity is T3.
      The only way out is `t3_whitelist` (a list of exact ids), and it leads to T2, never lower;
    - otherwise: exact `entity_tiers` entry, else the strictest matching glob, else the domain tier;
    - agent overrides can only make an entity stricter.
    """
    try:
        return _resolve(entity_id, DEFAULT_POLICY if policy is None else policy, agent_overrides or {})
    except Exception:  # noqa: BLE001 - the safety layer never fails open
        return "T3"


def _resolve(entity_id: str, policy: dict, agent_overrides: dict) -> str:
    if not isinstance(entity_id, str) or not _ENTITY_ID.fullmatch(entity_id):
        return "T3"
    domain = entity_id.split(".", 1)[0]
    domain_tiers = policy.get("domain_tiers")
    domain_tier = _tier(domain_tiers.get(domain)) if isinstance(domain_tiers, dict) else "T3"
    exact, globs = _matches(policy.get("entity_tiers"), entity_id)

    if "T3" in (domain_tier, exact, *globs):
        whitelist = policy.get("t3_whitelist")
        whitelisted = isinstance(whitelist, list | tuple | set | frozenset) and entity_id in whitelist
        tier = "T2" if whitelisted else "T3"
    elif exact is not None:
        tier = exact
    elif globs:
        tier = max(globs, key=_rank)
    else:
        tier = domain_tier

    override_exact, override_globs = _matches(agent_overrides, entity_id)
    for override in (override_exact, *override_globs):
        if override is not None and _rank(override) > _rank(tier):
            tier = override
    return tier


def schema_defaults() -> dict:
    """Defaults as written in the JSON Schema (used by the sync test)."""
    props = json.loads(_SCHEMA.read_text())["properties"]
    return {key: props[key]["default"] for key in ("domain_tiers", "entity_tiers", "t3_whitelist")}
