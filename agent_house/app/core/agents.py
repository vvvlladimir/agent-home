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
    except ValidationError as err:
        first = err.errors()[0]
        raise ConfigError(f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}") from None
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


def resolve_scope(agent: AgentConfig, ha, tier_of) -> tuple[list[str], list[str]]:
    """(entities shown in the prompt, further entities the agent may find via list_entities).

    Both lists together are the agent's allowlist. An explicit `entities` list with
    `entities_mode: only` is taken as is (minus ids HA does not know) and is shown in full; with
    `pinned` those entities go first and the room fills up the rest. Otherwise: every visible entity of the
    room except config/diagnostic helpers and device noise, ranked low tiers first, then T3,
    then room sensors (motion, temperature, …), then other read-only entities; the first
    `max_entities` go into the prompt. `tier_of(entity_id) -> tier`.
    """
    pinned = [e for e in agent.entities if e in ha.states]
    if agent.entities and agent.entities_mode == "only":
        return pinned, []
    registry = ha.registry
    candidates = [
        e
        for e in registry.entities_in(agent.room)
        if e in ha.states
        and not registry.is_hidden(e)
        and not registry.entity_meta.get(e, {}).get("entity_category")
        and not is_noise(ha, e)
    ]

    def order(entity_id: str) -> tuple[int, str]:
        # low tiers first (the everyday lights and fans), T3 last among actuators, sensors after
        if entity_id.split(".", 1)[0] in READ_ONLY_DOMAINS:
            rank = len(TIERS) + (0 if _device_class(ha, entity_id) in ROOM_SENSOR_CLASSES else 1)
        else:
            rank = TIERS.index(tier_of(entity_id))
        return rank, entity_id

    candidates.sort(key=order)
    # pinned: the ticked entities always come first, the room fills up the prompt and the search
    candidates = (pinned + [e for e in candidates if e not in pinned])[: max(SCOPE_HARD_CAP, len(pinned))]
    shown = max(agent.max_entities, len(pinned))
    return candidates[:shown], candidates[shown:]


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


def parse_policy(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ConfigError("the policy must be an object")
    try:
        return PolicyModel(**payload).model_dump(mode="json", exclude_none=True)
    except ValidationError as err:
        first = err.errors()[0]
        raise ConfigError(f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}") from None
