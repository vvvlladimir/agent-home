"""Global safety state: the kill switch and the global mode, persisted in the key/value store, and
the owner's policy file (`PUT api/policy`, validated against contracts/schemas/policy.schema.json).

Read fail-closed: anything that is not exactly "released" means engaged, anything that is not
exactly "live" means shadow.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator

from . import clock
from .policy import effective_policy

KILL_EVENT = "agent_house_kill_switch"


class Safety:
    def __init__(self, store, feed, data_dir: Path, t3_whitelist=()) -> None:
        self.store, self.feed = store, feed
        # The add-on option, never the policy file: nothing reachable from the API lifts a T3.
        self.t3_whitelist = sorted({str(e) for e in t3_whitelist})
        self.policy_file = Path(data_dir) / "policy.yaml"
        self._policy: dict | None = None

    # --- kill switch ----------------------------------------------------------------------
    @property
    def kill(self) -> dict:
        value = self.store.get("kill_switch", {"engaged": False, "since": None, "reason": None})
        if not isinstance(value, dict) or value.get("engaged") is not False:
            value = value if isinstance(value, dict) else {}
            return {"engaged": True, "since": value.get("since"), "reason": value.get("reason")}
        return {"engaged": False, "since": None, "reason": None}

    @property
    def kill_engaged(self) -> bool:
        return self.kill["engaged"]

    def set_kill(self, engaged: bool, reason: str | None = None, source: str = "the GUI") -> bool:
        """Returns True when the state changed."""
        if self.kill_engaged == engaged:
            return False
        self.store.set(
            "kill_switch",
            {
                "engaged": engaged,
                "since": clock.now(UTC).isoformat() if engaged else None,
                "reason": (reason or f"set in {source}")[:200] if engaged else None,
            },
        )
        self.feed.add(
            "system",
            f"Kill switch ENGAGED ({source})" if engaged else f"Kill switch released ({source})",
            level="warning",
            data={"reason": reason, "source": source},
        )
        return True

    # --- mode -----------------------------------------------------------------------------
    @property
    def mode(self) -> str:
        return "live" if self.store.get("mode", "shadow") == "live" else "shadow"

    def set_mode(self, mode: str) -> None:
        if mode not in ("shadow", "live"):
            raise ValueError("mode must be shadow or live")
        if self.mode != mode:
            self.store.set("mode", mode)
            self.feed.add(
                "system", f"Global mode set to {mode}", level="warning" if mode == "live" else "info"
            )

    # --- policy file ----------------------------------------------------------------------
    @property
    def policy(self) -> dict:
        if self._policy is None:
            stored: object = None
            try:
                if self.policy_file.is_file():
                    stored = yaml.safe_load(self.policy_file.read_text())
            except (OSError, ValueError, yaml.YAMLError):  # ValueError: not UTF-8
                # An unreadable policy must not widen anything: fall back to deny-everything.
                self._policy = {**effective_policy(None), "domain_tiers": {}, "t3_whitelist": []}
                return self._policy
            self._policy = {**effective_policy(stored), "t3_whitelist": list(self.t3_whitelist)}
        return self._policy

    def save_policy(self, policy: dict) -> dict:
        policy = {k: v for k, v in policy.items() if k != "t3_whitelist"}  # an add-on option
        temporary = self.policy_file.with_suffix(".yaml.tmp")
        temporary.write_text(yaml.safe_dump(policy, sort_keys=True))
        temporary.replace(self.policy_file)
        self._policy = None
        self.feed.add("system", "Policy changed", level="warning")
        return self.policy


# --- the policy file as the API takes it -------------------------------------------------------
EntityId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]+\.[a-z0-9_]+$")]
Tier = Literal["T0", "T1", "T2", "T3"]


class ConfigError(ValueError):
    pass


class PolicyModel(BaseModel):
    """Mirror of contracts/schemas/policy.schema.json for `PUT api/policy`."""

    model_config = ConfigDict(extra="forbid")

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
    an add-on option, never something the API or a model can do (QA-004)."""
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
