"""Global safety state: the kill switch and the global mode, persisted in the key/value store.

Read fail-closed: anything that is not exactly "released" means engaged, anything that is not
exactly "live" means shadow.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

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
                "since": datetime.now(UTC).isoformat() if engaged else None,
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
