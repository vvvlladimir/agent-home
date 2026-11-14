"""What the sim panel changes: model backends, the Jetson address, the house spec. Persisted in
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

ROOM_BACKENDS = ("fake", "jetson", "openai", "anthropic", "compatible", "claude")
CLOUD_BACKENDS = ("fake", "real", "claude")  # claude = `claude -p` (sim/claude_bridge.py)
METRICS_SOURCES = ("auto", "fake", "jetson")
_SECTIONS = ("room", "jetson", "anthropic", "openai", "house", "backfill_days")
SECRET_KEYS = ("anthropic_api_key", "openai_api_key", "compatible_api_key", "jetson_token")


@dataclass
class RoomLLM:
    backend: str = "fake"
    openai_model: str = "gpt-6-luna"
    anthropic_model: str = "claude-haiku-5-5"
    compatible_base_url: str = "https://api.deepinfra.com/v1/openai"
    compatible_model: str = "Qwen/Qwen3.5-4B"  # check the provider's catalogue for the exact id
    claude_model: str = "haiku"  # backend "claude": `claude -p --model`, no API key (D30)
    claude_effort: str = "low"
    pace: bool = True  # slow cloud answers down to the Jetson's measured speed (D23)
    prefill_tps: float = 515.0
    decode_tps: float = 16.0
    n_ctx: int = 8192
    single_slot: bool = True  # llama-server runs with -np 1: one request at a time


@dataclass
class Jetson:
    host: str = ""  # IP or hostname of the real Jetson, e.g. 192.168.178.40
    llm_port: int = 8080
    metrics_port: int = 9100
    metrics: str = "auto"  # auto = the real Jetson when the room backend is "jetson"

    def llm_url(self) -> str:
        return f"http://{self.host}:{self.llm_port}" if self.host else ""

    def metrics_url(self) -> str:
        return f"http://{self.host}:{self.metrics_port}" if self.host else ""


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
    room: RoomLLM = field(default_factory=RoomLLM)
    jetson: Jetson = field(default_factory=Jetson)
    anthropic: Cloud = field(default_factory=Cloud)
    openai: Cloud = field(default_factory=Cloud)
    house: dict = field(default_factory=dict)  # HouseSpec.to_dict()
    backfill_days: int = 14
    secrets: dict = field(default_factory=dict, repr=False)
    faults: dict[str, Faults] = field(
        default_factory=lambda: {k: Faults() for k in ("llm", "anthropic", "openai")}
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
            else:
                cfg.update(data)
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
        for section in ("room", "jetson", "anthropic", "openai"):
            if section in data:
                _merge(getattr(self, section), data[section], section)
        if "house" in data:
            if not isinstance(data["house"], dict):
                raise ValueError("house must be an object")
            self.house = dict(data["house"])
        if "backfill_days" in data:
            self.backfill_days = max(0, min(int(data["backfill_days"]), 30))
        if self.room.backend not in ROOM_BACKENDS:
            raise ValueError(f"room.backend must be one of {ROOM_BACKENDS}")
        if self.jetson.metrics not in METRICS_SOURCES:
            raise ValueError(f"jetson.metrics must be one of {METRICS_SOURCES}")
        for c in (self.anthropic, self.openai):
            if c.backend not in CLOUD_BACKENDS:
                raise ValueError(f"cloud backend must be one of {CLOUD_BACKENDS}")
        if self.jetson.host and not re.fullmatch(r"[A-Za-z0-9.\-]+|\[[0-9A-Fa-f:]+\]", self.jetson.host):
            raise ValueError("jetson.host: an IP address or a hostname, no scheme or port")
        efforts = ("low", "medium", "high", "xhigh", "max")
        if self.room.claude_effort not in efforts or self.anthropic.claude_effort not in efforts:
            raise ValueError(f"claude_effort must be one of {efforts}")
        if self.openai.backend == "claude":
            raise ValueError("the claude backend serves the Anthropic gateway only")
        if not re.fullmatch(r"https?://\S+", self.room.compatible_base_url):
            raise ValueError("room.compatible_base_url must start with http:// or https://")
        if not (0 < self.room.prefill_tps and 0 < self.room.decode_tps and 512 <= self.room.n_ctx <= 262144):
            raise ValueError("room: speeds must be > 0 and n_ctx within 512..262144")

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

    def metrics_source(self) -> str:
        if self.jetson.metrics == "auto":
            return "jetson" if self.room.backend == "jetson" and self.jetson.host else "fake"
        return self.jetson.metrics

    def public(self) -> dict:
        out = {
            "room": asdict(self.room),
            "jetson": asdict(self.jetson),
            "anthropic": asdict(self.anthropic),
            "openai": asdict(self.openai),
            "house": self.house,
            "backfill_days": self.backfill_days,
            "secrets": {k: mask(self.secrets.get(k, "")) for k in SECRET_KEYS},
            "faults": {k: asdict(v) for k, v in self.faults.items()},
            "metrics_source": self.metrics_source(),
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
