"""What the sim panel changes: the cloud backends, the learner (in the sim or the real Jetson), the
house spec. Persisted in
`.data/sim/` (gitignored): `config.json` for settings, `secrets.json` (mode 0600) for API keys.

Secrets never leave this module unmasked: `public()` shows the last four characters only.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CLOUD_BACKENDS = ("fake", "real", "claude")  # claude = `claude -p` (sim/claude_bridge.py)
LEARNER_BACKENDS = ("sim", "jetson", "off")  # sim = jetson/learner/server.py in this process
_SECTIONS = ("jetson", "anthropic", "openai", "house", "backfill_days")
SECRET_KEYS = ("anthropic_api_key", "openai_api_key", "jetson_token")


@dataclass
class Jetson:
    learner: str = "sim"  # off = the learner is down: the add-on falls back to predict.py
    host: str = ""  # IP or hostname of the real Jetson, e.g. 192.168.178.40
    learner_port: int = 9200

    def learner_url(self) -> str:
        return f"http://{self.host}:{self.learner_port}" if self.host else ""


@dataclass
class Cloud:
    backend: str = "fake"  # real = forward to the provider with the key from the panel
    model_override: str = ""  # empty = the model the add-on asked for
    claude_model: str = "sonnet"  # backend "claude" (Anthropic gateway only): `claude -p` (D30)
    claude_effort: str = "medium"


@dataclass
class Faults:
    fail_next: int = 0
    status: int = 500
    latency_s: float = 0.0

    def take(self) -> int | None:
        if self.fail_next > 0:
            self.fail_next -= 1
            return self.status
        return None


@dataclass
class SimConfig:
    jetson: Jetson = field(default_factory=Jetson)
    anthropic: Cloud = field(default_factory=Cloud)
    openai: Cloud = field(default_factory=Cloud)
    house: dict = field(default_factory=dict)  # HouseSpec.to_dict()
    backfill_days: int = 14
    secrets: dict = field(default_factory=dict, repr=False)
    faults: dict[str, Faults] = field(
        default_factory=lambda: {k: Faults() for k in ("learner", "anthropic", "openai")}
    )
    path: Path | None = field(default=None, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ---- persistence ---------------------------------------------------------------------------
    @classmethod
    def load(cls, directory: Path | None) -> SimConfig:
        cfg = cls(path=directory)
        if not directory:
            return cfg
        for name, target in (("config.json", None), ("secrets.json", "secrets")):
            f = directory / name
            if not f.is_file():
                continue
            try:
                data = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if target == "secrets":
                cfg.secrets = {k: v for k, v in data.items() if k in SECRET_KEYS and isinstance(v, str)}
                continue
            try:
                cfg.update(data)
            except (ValueError, TypeError):  # a config from an older panel (room agents, D40)
                jetson = data.get("jetson") if isinstance(data.get("jetson"), dict) else {}
                known = {k: v for k, v in jetson.items() if k in Jetson.__dataclass_fields__}
                cfg.update({**{k: data[k] for k in ("anthropic", "openai", "house", "backfill_days")
                               if k in data}, "jetson": known})  # fmt: skip
        return cfg

    def save(self) -> None:
        if not self.path:
            return
        self.path.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in self.public().items() if k not in ("secrets", "faults")}
        (self.path / "config.json").write_text(json.dumps(data, indent=2, sort_keys=True))
        f = self.path / "secrets.json"
        tmp = f.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(self.secrets, out)
        tmp.replace(f)

    # ---- changes from the panel ----------------------------------------------------------------
    def update(self, data: dict[str, Any]) -> None:
        """Apply a partial update atomically; unknown keys and bad values raise ValueError."""
        with self.lock:
            saved = {k: copy.deepcopy(getattr(self, k)) for k in _SECTIONS}
            try:
                self._apply(data)
            except (ValueError, TypeError):
                for k, v in saved.items():
                    setattr(self, k, v)
                raise

    def _apply(self, data: dict[str, Any]) -> None:
        unknown = sorted(set(data) - set(_SECTIONS))
        if unknown:
            raise ValueError(f"unknown setting {unknown[0]} (known: {', '.join(_SECTIONS)})")
        for section in ("jetson", "anthropic", "openai"):
            if section in data:
                _merge(getattr(self, section), data[section], section)
        if "house" in data:
            if not isinstance(data["house"], dict):
                raise ValueError("house must be an object")
            self.house = dict(data["house"])
        if "backfill_days" in data:
            self.backfill_days = max(0, min(int(data["backfill_days"]), 30))
        if self.jetson.learner not in LEARNER_BACKENDS:
            raise ValueError(f"jetson.learner must be one of {LEARNER_BACKENDS}")
        for c in (self.anthropic, self.openai):
            if c.backend not in CLOUD_BACKENDS:
                raise ValueError(f"cloud backend must be one of {CLOUD_BACKENDS}")
        if self.jetson.host and not re.fullmatch(r"[A-Za-z0-9.\-]+|\[[0-9A-Fa-f:]+\]", self.jetson.host):
            raise ValueError("jetson.host: an IP address or a hostname, no scheme or port")
        efforts = ("low", "medium", "high", "xhigh", "max")
        if self.anthropic.claude_effort not in efforts:
            raise ValueError(f"claude_effort must be one of {efforts}")
        if self.openai.backend == "claude":
            raise ValueError("the claude backend serves the Anthropic gateway only")

    def set_secret(self, key: str, value: str) -> None:
        if key not in SECRET_KEYS:
            raise ValueError(f"unknown secret {key!r}")
        value = value.strip()
        with self.lock:
            if value:
                self.secrets[key] = value
            else:
                self.secrets.pop(key, None)

    def secret(self, key: str) -> str:
        return self.secrets.get(key, "")

    def public(self) -> dict:
        out = {
            "jetson": asdict(self.jetson),
            "anthropic": asdict(self.anthropic),
            "openai": asdict(self.openai),
            "house": self.house,
            "backfill_days": self.backfill_days,
            "secrets": {k: mask(self.secrets.get(k, "")) for k in SECRET_KEYS},
            "faults": {k: asdict(v) for k, v in self.faults.items()},
        }
        return out


def mask(value: str) -> str:
    if not value:
        return ""
    return f"…{value[-4:]}" if len(value) > 8 else "set"


def _merge(obj: Any, data: Any, section: str) -> None:
    if not isinstance(data, dict):
        raise ValueError(f"{section} must be an object")
    for k, v in data.items():
        if not hasattr(obj, k):
            raise ValueError(f"unknown setting {section}.{k}")
        cur = getattr(obj, k)
        try:
            if isinstance(cur, bool):
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
            elif isinstance(cur, int):
                v = int(v)
            elif isinstance(cur, float):
                v = float(v)
            else:
                v = str(v).strip()
        except (TypeError, ValueError) as err:
            raise ValueError(f"bad value for {section}.{k}: {v!r}") from err
        setattr(obj, k, v)
