"""Room agent configuration: the pydantic mirror of contracts/schemas/agent_config.schema.json,
its YAML store in /data/agents, and the agent's entity scope (its allowlist).

The JSON Schema is the source of truth; tests/unit/test_agents.py keeps the two in sync.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator

from .policy import READ_ONLY_DOMAINS, TIERS

log = logging.getLogger(__name__)

EntityId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]+\.[a-z0-9_]+$")]
Tier = Literal["T0", "T1", "T2", "T3"]
_ID = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class EventRule(_Strict):
    entities: list[str] = Field(min_length=1)
    to: list[str] | None = None
    from_: list[str] | None = Field(default=None, alias="from")
    min_change: float | None = Field(default=None, ge=0)
    above: float | None = None
    below: float | None = None
    debounce_s: float = Field(default=5, ge=0)
    cooldown_s: float = Field(default=120, ge=0)
    priority: Literal["alarm", "routine"] = "routine"
    reason: str | None = Field(default=None, max_length=120)


class CronRule(_Strict):
    expr: str
    reason: str | None = Field(default=None, max_length=120)

    @field_validator("expr")
    @classmethod
    def _five_fields(cls, value: str) -> str:
        if len(value.split()) != 5 or not croniter.is_valid(value):
            raise ValueError("not a 5-field cron expression")
        return value


class WakeRules(_Strict):
    events: list[EventRule] = Field(default_factory=list)
    cron: list[CronRule] = Field(default_factory=list)
    max_wakes_per_hour: int = Field(default=12, ge=0, le=120)


class Limits(_Strict):
    max_steps: int = Field(default=5, ge=1, le=12)
    max_output_tokens: int = Field(default=512, ge=32, le=4096)
    context_budget_tokens: int = Field(default=3000, ge=512, le=16384)
    timeout_s: float = Field(default=45, ge=5, le=300)
    max_actions_per_minute: int = Field(default=6, ge=0, le=60)


class AgentConfig(_Strict):
    id: str = Field(pattern=_ID.pattern)
    name: str = Field(min_length=1, max_length=64)
    room: str
    enabled: bool = True
    mode: Literal["shadow", "live"] = "shadow"
    entities: list[EntityId] = Field(default_factory=list, max_length=25)
    entities_mode: Literal["only", "pinned"] = "only"
    max_entities: int = Field(default=20, ge=1, le=25)
    tier_overrides: dict[str, Tier] = Field(default_factory=dict)
    system_prompt: str = Field(default="", max_length=4000)
    model_profile: str = ""
    wake_rules: WakeRules = Field(default_factory=WakeRules)
    limits: Limits = Field(default_factory=Limits)
    memory_cap_chars: int = Field(default=1200, ge=0, le=4000)

    def dump(self) -> dict:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class ConfigError(ValueError):
    pass


def parse_agent(payload: object) -> AgentConfig:
    """Validate one agent config. Raises ConfigError with a one-line reason."""
    if not isinstance(payload, dict):
        raise ConfigError("the agent config must be an object")
    try:
        agent = AgentConfig(**payload)
    except ValidationError as err:  # every problem at once, not one per attempt (QA-004)
        problems = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in err.errors()[:6]]
        raise ConfigError("; ".join(problems)) from None
    if agent.id == "orchestrator":
        raise ConfigError("id: 'orchestrator' is reserved for the orchestrator")
    if agent.entities and agent.entities_mode == "only":
        for index, rule in enumerate(agent.wake_rules.events):
            for pattern in rule.entities:
                if not any(c in pattern for c in "*?[") and pattern not in agent.entities:
                    raise ConfigError(f"wake_rules.events.{index}: {pattern} is not in the entity allowlist")
    return agent


class AgentStore:
    """One YAML file per agent in <data>/agents. A file that does not validate is skipped, loudly."""

    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir) / "agents"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.agents: dict[str, AgentConfig] = {}
        for file in sorted(self.dir.glob("*.yaml")):
            try:
                agent = parse_agent(yaml.safe_load(file.read_text()))
                if agent.id != file.stem:
                    raise ConfigError(f"id {agent.id!r} does not match the file name")
                self.agents[agent.id] = agent
            except (OSError, yaml.YAMLError, ConfigError) as err:
                log.error("ignoring agent file %s: %s", file.name, err)

    def get(self, agent_id: str) -> AgentConfig | None:
        return self.agents.get(agent_id)

    def list(self) -> list[AgentConfig]:
        return [self.agents[key] for key in sorted(self.agents)]

    def save(self, agent: AgentConfig) -> None:
        file = self.dir / f"{agent.id}.yaml"  # the id pattern rules out path tricks
        temporary = file.with_suffix(".yaml.tmp")
        temporary.write_text(yaml.safe_dump(agent.dump(), sort_keys=False, allow_unicode=True))
        temporary.replace(file)
        self.agents[agent.id] = agent

    def delete(self, agent_id: str) -> bool:
        if not _ID.fullmatch(agent_id) or agent_id not in self.agents:
            return False
        (self.dir / f"{agent_id}.yaml").unlink(missing_ok=True)
        del self.agents[agent_id]
        return True


# Network-level switches nothing of ours should flip or learn from: opening the Zigbee / Z-Wave /
# Thread network to new devices. Usually `entity_category: config` too, but not always.
NEVER_OWNED = re.compile(r"_(permit_join|permit_joining|inclusion_mode|commissioning)$")
# Noise in a real HA: device bookkeeping, not anything a room agent reasons about.
NOISE_DOMAINS = frozenset({"update", "device_tracker"})
NOISE_DEVICE_CLASSES = frozenset({"battery", "signal_strength", "voltage", "connectivity", "update"})
NOISE_SUFFIX = re.compile(
    r"_(linkquality|rssi|signal|signal_strength|last_seen|uptime|firmware|update_state|battery|"
    r"battery_low|voltage|power_on_behavior|identify)$"
)
# Sensors that describe the room itself come before the other read-only entities.
ROOM_SENSOR_CLASSES = frozenset(
    {"motion", "occupancy", "presence", "door", "window", "opening", "temperature", "humidity",
     "illuminance", "smoke", "moisture", "gas", "carbon_monoxide"}
)  # fmt: skip
SCOPE_HARD_CAP = 200  # what an agent may reach via list_entities; the prompt shows max_entities


def _device_class(ha, entity_id: str) -> str | None:
    attrs = (ha.states.get(entity_id) or {}).get("attributes") or {}
    meta = ha.registry.entity_meta.get(entity_id, {})
    return attrs.get("device_class") or meta.get("device_class") or meta.get("original_device_class")


def is_noise(ha, entity_id: str) -> bool:
    """Device bookkeeping (battery, signal, firmware, …) that only fills an agent's context."""
    domain = entity_id.split(".", 1)[0]
    if domain in NOISE_DOMAINS or NOISE_SUFFIX.search(entity_id):
        return True
    return domain in READ_ONLY_DOMAINS and _device_class(ha, entity_id) in NOISE_DEVICE_CLASSES


ROOM_SENSOR_SUFFIX = re.compile(
    r"_(motion|occupancy|presence|door|window|contact|temperature|humidity|illuminance|smoke|leak)$"
)
# Indicator LEDs of sensor boxes (ESPHome `*_rgb_light`, status LEDs): lights nobody asks for.
INDICATOR = re.compile(r"_(rgb_light|status_led|status_light|led|indicator|backlight)$")
# Device settings (delays, countdowns, modes): T3 by default, rarely what a room agent reads.
SETTING_DOMAINS = frozenset({"number", "select", "text", "button", "time", "date", "datetime"})
UNAVAILABLE = frozenset({"unavailable", "unknown"})


def is_room_sensor(ha, entity_id: str) -> bool:
    return entity_id.split(".", 1)[0] in READ_ONLY_DOMAINS and (
        _device_class(ha, entity_id) in ROOM_SENSOR_CLASSES or bool(ROOM_SENSOR_SUFFIX.search(entity_id))
    )


def resolve_scope(
    agent: AgentConfig, ha, tier_of, usage: dict[str, int] | None = None
) -> tuple[list[str], list[str]]:
    """(entities shown in the prompt, further entities the agent may find via list_entities).

    Both lists together are the agent's allowlist. An explicit `entities` list with
    `entities_mode: only` is taken as is (minus ids HA does not know) and is shown in full; with
    `pinned` those entities go first and the room fills up the rest. Otherwise every visible
    entity of the room except config/diagnostic helpers and device noise, in this order:
    entities named in the agent's wake rules; entities the agent used before (`usage`:
    entity_id → uses, most used first); available actuators by tier (T0 first); room sensors
    (motion, temperature, …); other sensors; last indicator LEDs and unavailable entities.
    A quarter of the prompt is kept for room sensors so the agent can always see its room.
    `tier_of(entity_id) -> tier`.
    """
    pinned = [e for e in agent.entities if e in ha.states]
    if agent.entities and agent.entities_mode == "only":
        return pinned, []
    usage = usage or {}
    registry = ha.registry
    candidates = [
        e
        for e in registry.entities_in(agent.room)
        if e in ha.states
        and not registry.is_hidden(e)
        and not registry.entity_meta.get(e, {}).get("entity_category")
        and not is_noise(ha, e)
    ]
    watched = {e for rule in agent.wake_rules.events for e in rule.entities}

    def order(entity_id: str) -> tuple[int, int, str]:
        if entity_id in watched:
            return 0, 0, entity_id
        if usage.get(entity_id):
            return 1, -usage[entity_id], entity_id
        state = str((ha.states.get(entity_id) or {}).get("state"))
        if state in UNAVAILABLE or INDICATOR.search(entity_id):
            return 20, 0, entity_id
        if entity_id.split(".", 1)[0] in READ_ONLY_DOMAINS:
            return (10 if is_room_sensor(ha, entity_id) else 11), 0, entity_id
        tier = tier_of(entity_id)
        if tier == "T3" and entity_id.split(".", 1)[0] in SETTING_DOMAINS:
            # device settings the agent can never change (`number.*_off_delay`): after the sensors
            return 12, 0, entity_id
        return 2 + TIERS.index(tier), 0, entity_id

    candidates.sort(key=order)
    # pinned: the ticked entities always come first, the room fills up the prompt and the search
    candidates = (pinned + [e for e in candidates if e not in pinned])[: max(SCOPE_HARD_CAP, len(pinned))]
    size = max(agent.max_entities, len(pinned))
    shown = candidates[:size]
    # keep a quarter of the prompt for room sensors: without them "check your room" is blind
    sensors = [e for e in candidates[size:] if is_room_sensor(ha, e) and order(e)[0] < 20]
    missing = max(0, agent.max_entities // 4 - sum(is_room_sensor(ha, e) for e in shown))
    keep = set(pinned) | {e for e in shown if order(e)[0] <= 1}
    for sensor in sensors[:missing]:
        victim = next((e for e in reversed(shown) if e not in keep and not is_room_sensor(ha, e)), None)
        if victim is None:
            break
        shown[shown.index(victim)] = sensor
    shown.sort(key=lambda e: (0, pinned.index(e), ()) if e in pinned else (1, 0, order(e)))
    rest = [e for e in candidates if e not in set(shown)]
    return shown, rest


class PolicyModel(_Strict):
    """Mirror of contracts/schemas/policy.schema.json for `PUT api/policy`."""

    domain_tiers: dict[Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]+$")], Tier] | None = None
    entity_tiers: dict[str, Tier] | None = None
    t3_whitelist: list[EntityId] | None = None
    allowed_services: dict[str, list[Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]+$")]]] | None = (
        None
    )
    rate_limits: dict[Literal["per_entity_per_minute", "global_per_minute"], int] | None = None
    approval_ttl_s: int | None = Field(default=None, ge=30)

    @field_validator("rate_limits")
    @classmethod
    def _positive(cls, value: dict | None) -> dict | None:
        if value and any(v < 1 for v in value.values()):
            raise ValueError("rate limits must be at least 1")
        return value


T3_WHITELIST_HINT = (
    "t3_whitelist is an add-on option, read-only here: set it in Home Assistant under Settings → Apps "
    "(Add-ons before HA 2026.2) → Agent House → Configuration"
)


def check_whitelist(payload: object, current: list[str]) -> None:
    """A policy body may carry `t3_whitelist` only unchanged (as GET returned it): lifting a T3 is
    an add-on option, never something the API or an agent can do (QA-004)."""
    if isinstance(payload, dict) and "t3_whitelist" in payload:
        given = payload["t3_whitelist"]
        if not isinstance(given, list) or sorted({str(e) for e in given}) != sorted(current):
            raise ConfigError(T3_WHITELIST_HINT)


def parse_policy(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ConfigError("the policy must be an object")
    try:
        return PolicyModel(**payload).model_dump(mode="json", exclude_none=True)
    except ValidationError as err:
        first = err.errors()[0]
        raise ConfigError(f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}") from None
