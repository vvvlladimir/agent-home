"""Policy engine: tier resolution and the decision function (pure functions, no I/O).

Tiers: T0 free · T1 act + notify · T2 act only after human approval · T3 never.
Deny by default: a domain that is not listed is T3.

`decide` answers one question for one service call on one entity, in a fixed order:
kill switch → allowlist → ownership → service allowlist → tier → rate limit → mode. Anything
unexpected is
`denied`. `core/actuator.py` is its only production caller; changes here need a `reviewer` pass.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
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
        "scene": "T2",
        "input_boolean": "T2",
        "input_select": "T2",
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
    # the house mode helper (M5 council): the orchestrator switches it, the owner is notified
    "entity_tiers": {"cover.*garage*": "T3", "input_select.agent_house_mode": "T1"},
    "t3_whitelist": [],
    "allowed_services": {
        "light": ["turn_on", "turn_off", "toggle"],
        "media_player": [
            "turn_on",
            "turn_off",
            "media_play",
            "media_pause",
            "media_stop",
            "volume_set",
            "volume_mute",
        ],
        "fan": ["turn_on", "turn_off", "set_percentage"],
        "scene": ["turn_on"],
        "input_boolean": ["turn_on", "turn_off"],
        "input_select": ["select_option"],
        "humidifier": ["turn_on", "turn_off", "set_humidity"],
        "vacuum": ["start", "return_to_base"],
        "climate": ["set_temperature", "set_hvac_mode", "turn_on", "turn_off"],
        "cover": ["open_cover", "close_cover", "set_cover_position", "stop_cover"],
        "switch": ["turn_on", "turn_off"],
        "water_heater": ["set_temperature"],
        "input_number": ["set_value"],
    },
    "rate_limits": {"per_entity_per_minute": 4, "global_per_minute": 30},
    "approval_ttl_s": 900,
}
POLICY_KEYS = tuple(DEFAULT_POLICY)


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
    defaults = {key: props[key]["default"] for key in POLICY_KEYS}
    limits = props["rate_limits"]["properties"]
    defaults["rate_limits"] = {key: limits[key]["default"] for key in limits}
    return defaults


def effective_policy(stored: object) -> dict:
    """The owner's policy file over the defaults, key by key. A missing key keeps its default;
    a present key replaces the default as a whole (so the owner can remove a domain)."""
    stored = stored if isinstance(stored, dict) else {}
    merged = {key: stored.get(key, default) for key, default in DEFAULT_POLICY.items()}
    if isinstance(merged["rate_limits"], dict):
        merged["rate_limits"] = {**DEFAULT_POLICY["rate_limits"], **merged["rate_limits"]}
    return merged


# --- decision ---------------------------------------------------------------------------------

_NAME = re.compile(r"^[a-z0-9_]+$")
# Service data must not carry its own target: the entity is the one the decision was made for.
TARGET_KEYS = frozenset({"entity_id", "area_id", "device_id", "floor_id", "label_id", "target"})


@dataclass(frozen=True)
class Decision:
    verdict: str  # allow | shadow | pending | denied
    tier: str
    reason: str


def _limit(value: object, default: int) -> int:
    """A rate limit as a non-negative int; anything malformed means 0 (= nothing allowed)."""
    if value is None:
        return default
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def decide(
    *,
    domain: object,
    service: object,
    entity_id: object,
    data: object,
    agent: dict | None,
    scope: object,
    kill_engaged: bool,
    global_mode: str,
    policy: dict | None = None,
    recent: dict | None = None,
    approved: bool = False,
    force_shadow: bool = False,
    protected: object = (),
    owner: object = None,
) -> Decision:
    """May `agent` call `domain.service` on `entity_id` right now?

    - `agent`: the agent's config as a dict (`enabled`, `mode`, `tier_overrides`, `limits`);
    - `scope`: the entity ids the agent is allowed to see and touch (its allowlist);
    - `recent`: actions counted in the last minute: {"entity": n, "agent": n, "global": n};
    - `approved`: the owner approved this very action (lets a T2 action through, nothing else);
    - `force_shadow`: replays never actuate;
    - `protected`: entity ids no agent may ever actuate (the kill switch helper);
    - `owner`: the id of the agent that owns `entity_id` (M5 council: exactly one owner per entity).
      Only the owner may actuate; anything but the agent's own id (None included) denies.

    Verdicts: `denied`; `shadow` (would be allowed, the mode says do not send); `pending` (T2,
    waits for the owner); `allow`. Invariants (tests/unit/test_policy_decide.py): an engaged kill
    switch denies everything; T3 is never anything but denied; nothing outside the allowlist or
    the service allowlist gets past `denied`; nothing an agent does not own gets past `denied`;
    any exception or malformed input denies.
    """
    try:
        return _decide(
            domain, service, entity_id, data, agent, scope, kill_engaged, global_mode,
            DEFAULT_POLICY if policy is None else policy, recent or {}, approved, force_shadow, protected,
            owner,
        )  # fmt: skip
    except Exception:  # noqa: BLE001 - the safety layer never fails open
        return Decision("denied", "T3", "policy error (failing closed)")


def _decide(
    domain, service, entity_id, data, agent, scope, kill_engaged, global_mode, policy, recent, approved,
    force_shadow, protected, owner,
) -> Decision:  # fmt: skip
    def deny(reason: str, tier: str = "T3") -> Decision:
        return Decision("denied", tier, reason)

    # 1. kill switch: `is not False` so that a broken flag (None, "no") still stops everything
    if kill_engaged is not False:
        return deny("kill switch engaged")

    # shape: nothing below may see a value of the wrong type
    if not isinstance(entity_id, str) or not _ENTITY_ID.fullmatch(entity_id):
        return deny("malformed entity id")
    if not isinstance(domain, str) or not isinstance(service, str):
        return deny("malformed domain or service")
    if not _NAME.fullmatch(domain) or not _NAME.fullmatch(service):
        return deny("malformed domain or service")
    if not isinstance(data, dict):
        return deny("service data must be an object")
    if TARGET_KEYS & set(data):
        return deny("service data must not name a target; one call acts on one entity_id")
    if not isinstance(agent, dict) or agent.get("enabled") is not True:
        return deny("agent is disabled or unknown")
    tier = resolve_tier(entity_id, policy, agent.get("tier_overrides") or {})

    # 2. the agent's entity allowlist
    if not isinstance(scope, set | frozenset) or entity_id not in scope:
        return deny(f"{entity_id} is not in this agent's allowlist", tier)
    if not isinstance(protected, set | frozenset | tuple | list) or entity_id in protected:
        return deny(f"{entity_id} is reserved for the owner", tier)
    # 2b. ownership: one owner agent per entity; a neighbour asks the orchestrator instead
    agent_id = agent.get("id")
    if not isinstance(agent_id, str) or not agent_id or owner != agent_id:
        held = owner if isinstance(owner, str) and owner else "nobody"
        return deny(f"{entity_id} is owned by {held}, not by this agent; ask through ask_agent", tier)

    # 3. service allowlist (a call always targets an entity of its own domain)
    if domain != entity_id.split(".", 1)[0]:
        return deny(f"{domain}.{service} does not apply to {entity_id}", tier)
    services = policy.get("allowed_services")
    allowed = services.get(domain) if isinstance(services, dict) else None
    if not isinstance(allowed, list | tuple) or service not in allowed:
        return deny(f"service {domain}.{service} is not on the allowlist", tier)

    # 4. tier
    if tier not in ("T0", "T1", "T2"):
        return deny("T3 entities are never actuated", "T3")

    # 5. rate limits
    limits = policy.get("rate_limits")
    limits = limits if isinstance(limits, dict) else {}
    defaults = DEFAULT_POLICY["rate_limits"]
    agent_limits = agent.get("limits") if isinstance(agent.get("limits"), dict) else {}
    for key, cap, label in (
        ("entity", _limit(limits.get("per_entity_per_minute"), defaults["per_entity_per_minute"]), "entity"),
        ("agent", _limit(agent_limits.get("max_actions_per_minute"), 6), "agent"),
        ("global", _limit(limits.get("global_per_minute"), defaults["global_per_minute"]), "global"),
    ):
        count = recent.get(key, 0)
        if not isinstance(count, int) or count >= cap:
            return deny(f"rate limit: {label} limit of {cap} actions per minute reached", tier)

    # 6. mode: live needs both the global switch and the agent's own
    if force_shadow or global_mode != "live" or agent.get("mode") != "live":
        return Decision("shadow", tier, "shadow mode: recorded, not sent")
    if tier == "T2" and approved is not True:
        return Decision("pending", tier, "T2: waiting for the owner's approval")
    return Decision("allow", tier, "approved by the owner" if tier == "T2" else f"{tier}: allowed")
