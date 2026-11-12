"""Policy engine, part 1: tier resolution (pure functions, no I/O).

Tiers: T0 free · T1 act + notify · T2 act only after human approval · T3 never.
Deny by default: a domain that is not listed is T3.

The decision function (kill switch, allowlists, shadow mode, rate limits, approvals) is added
in M3 on top of `resolve_tier`; changes to this file need a `reviewer` pass.
"""

from __future__ import annotations

import json
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


def _rank(tier: str) -> int:
    return TIERS.index(tier)


def _stricter(a: str, b: str) -> str:
    return a if _rank(a) >= _rank(b) else b


def _match(patterns: dict[str, str], entity_id: str) -> str | None:
    """Exact id wins; otherwise the strictest matching glob (order in the file must not matter)."""
    if entity_id in patterns:
        return patterns[entity_id]
    hits = [tier for pattern, tier in patterns.items() if fnmatchcase(entity_id, pattern)]
    return max(hits, key=_rank) if hits else None


def resolve_tier(
    entity_id: str, policy: dict | None = None, agent_overrides: dict[str, str] | None = None
) -> str:
    """Tier of one entity under the global policy plus an agent's overrides.

    Invariants (tested):
    - unknown domain → T3;
    - an entity whose *domain* is T3 can only leave T3 through `t3_whitelist` (exact id), and
      then lands on T2 at best — never on T0/T1, whatever `entity_tiers` says;
    - agent overrides can only make an entity stricter.
    """
    policy = policy or DEFAULT_POLICY
    if "." not in entity_id:
        return "T3"
    domain = entity_id.split(".", 1)[0]
    domain_tier = policy.get("domain_tiers", {}).get(domain, "T3")
    entity_tier = _match(policy.get("entity_tiers", {}), entity_id)

    if domain_tier == "T3":
        if entity_id in policy.get("t3_whitelist", []):
            tier = _stricter("T2", entity_tier) if entity_tier in TIERS else "T2"
        else:
            tier = "T3"
    elif entity_tier == "T3" and entity_id in policy.get("t3_whitelist", []):
        tier = "T2"  # e.g. a garage cover the owner explicitly whitelisted
    else:
        tier = entity_tier if entity_tier in TIERS else domain_tier

    override = _match(agent_overrides or {}, entity_id)
    if override in TIERS:
        tier = _stricter(tier, override)
    return tier


def schema_defaults() -> dict:
    """Defaults as written in the JSON Schema (used by the sync test)."""
    props = json.loads(_SCHEMA.read_text())["properties"]
    return {key: props[key]["default"] for key in ("domain_tiers", "entity_tiers", "t3_whitelist")}
