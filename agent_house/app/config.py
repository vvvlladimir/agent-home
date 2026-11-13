"""Effective configuration.

Precedence (lowest → highest): built-in defaults → add-on options (`/data/options.json`,
written by the Supervisor from the HA Configuration tab) → `AH_*` environment variables
(used by the simulator and tests).

GUI-editable settings (`RuntimeSettings`) are separate: they live in `/data/settings.yaml` and
are changed through `PUT api/settings`.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger(__name__)

SUPERVISOR_INGRESS_IP = "172.30.32.2"  # the only peer HA ingress connects from
SECRET_FIELDS = ("ha_token", "anthropic_api_key", "jetson_token")


URL_FIELDS = ("ha_url", "ha_ws_url", "jetson_llm_url", "jetson_metrics_url", "anthropic_base_url")


def redact_url(url: str) -> str:
    """Keep scheme, host, port and path; drop credentials, query and fragment."""
    if not url:
        return url
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parts.port}" if parts.port else host
    hidden = "?***" if parts.query or parts.fragment else ""
    return (
        f"{parts.scheme}://{'***@' if parts.username or parts.password else ''}{netloc}{parts.path}{hidden}"
    )


class Settings(BaseModel):
    data_dir: Path = Path("/data")
    port: int = 8099
    log_level: str = "info"

    # Home Assistant. Inside the add-on these defaults go through the Supervisor proxy.
    ha_url: str = "http://supervisor/core"
    ha_ws_url: str = ""  # derived from ha_url when empty
    ha_token: str = ""

    # Peers allowed to connect. "*" disables the check (never do that on a real install).
    allowed_peers: list[str] = Field(default_factory=lambda: [SUPERVISOR_INGRESS_IP])

    # Jetson (empty = not configured yet)
    jetson_llm_url: str = ""
    jetson_metrics_url: str = ""
    jetson_token: str = ""

    # Orchestrator
    orchestrator_provider: str = "auto"  # anthropic | local | auto
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-5-5"  # D11
    anthropic_base_url: str = ""  # only the simulator sets this
    daily_budget_eur: float = 1.0
    redact_cloud: bool = True

    @property
    def via_supervisor(self) -> bool:
        return urlsplit(self.ha_url).hostname == "supervisor"

    @property
    def ha_rest_url(self) -> str:
        return self.ha_url.rstrip("/") + "/api"

    @property
    def ha_websocket_url(self) -> str:
        if self.ha_ws_url:
            return self.ha_ws_url
        base = self.ha_url.rstrip("/")
        scheme = "wss" if base.startswith("https") else "ws"
        rest = base.split("://", 1)[1]
        # Supervisor proxy: ws://supervisor/core/websocket; plain Core: ws://host:8123/api/websocket
        return f"{scheme}://{rest}/websocket" if self.via_supervisor else f"{scheme}://{rest}/api/websocket"

    def masked(self) -> dict:
        data = self.model_dump(mode="json")
        for key in SECRET_FIELDS:
            data[key] = "***" if data.get(key) else ""
        for key in URL_FIELDS:
            data[key] = redact_url(data.get(key) or "")
        return data


class RuntimeSettings(BaseModel):
    """Settings the owner changes in the GUI. Unknown keys are rejected, values are range-checked."""

    model_config = ConfigDict(extra="forbid")

    llm_timeout_s: float = Field(default=120.0, ge=5, le=900)  # one request, including retries
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_stale_after_s: float = Field(
        default=120.0, ge=1, le=3600
    )  # queued agent wakes older than this are dropped
    llm_max_queue: int = Field(default=16, ge=1, le=100)
    # None = the active profile's value. The benchmark compares the vendor's 1.5 against 0.
    llm_presence_penalty: float | None = Field(default=None, ge=0, le=2)
    # An HA helper that mirrors the kill switch: on = engaged, off = released. No agent may touch it.
    kill_switch_entity: str | None = Field(default=None, pattern=r"^input_boolean\.[a-z0-9_]+$")


def load_runtime(data_dir: Path) -> RuntimeSettings:
    file = data_dir / "settings.yaml"
    if not file.is_file():
        return RuntimeSettings()
    try:
        return RuntimeSettings(**(yaml.safe_load(file.read_text()) or {}))
    except (OSError, yaml.YAMLError, ValidationError, TypeError) as err:
        log.warning("ignoring %s, using defaults: %s", file, str(err).splitlines()[0])
        return RuntimeSettings()


def save_runtime(data_dir: Path, runtime: RuntimeSettings) -> None:
    file = data_dir / "settings.yaml"
    temporary = file.with_suffix(".yaml.tmp")
    temporary.write_text(yaml.safe_dump(runtime.model_dump(mode="json"), sort_keys=True))
    temporary.replace(file)


_ENV = {
    "AH_DATA_DIR": "data_dir",
    "AH_PORT": "port",
    "AH_LOG_LEVEL": "log_level",
    "AH_HA_URL": "ha_url",
    "AH_HA_WS_URL": "ha_ws_url",
    "AH_HA_TOKEN": "ha_token",
    "AH_ALLOWED_PEERS": "allowed_peers",
    "AH_LLM_URL": "jetson_llm_url",
    "AH_JETSON_METRICS_URL": "jetson_metrics_url",
    "AH_JETSON_TOKEN": "jetson_token",
    "AH_ORCHESTRATOR_PROVIDER": "orchestrator_provider",
    "AH_ANTHROPIC_API_KEY": "anthropic_api_key",
    "AH_ANTHROPIC_MODEL": "anthropic_model",
    "AH_ANTHROPIC_BASE_URL": "anthropic_base_url",
    "AH_DAILY_BUDGET_EUR": "daily_budget_eur",
    "AH_REDACT_CLOUD": "redact_cloud",
}
_OPTION_KEYS = (
    "log_level",
    "jetson_llm_url",
    "jetson_metrics_url",
    "jetson_token",
    "orchestrator_provider",
    "anthropic_api_key",
    "anthropic_model",
    "daily_budget_eur",
    "redact_cloud",
)


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    values: dict = {}

    data_dir = Path(env.get("AH_DATA_DIR", "/data"))
    options_file = data_dir / "options.json"
    if options_file.is_file():
        try:
            options = json.loads(options_file.read_text())
            values.update({k: options[k] for k in _OPTION_KEYS if options.get(k) not in (None, "")})
        except (OSError, ValueError) as err:
            log.warning("could not read %s: %s", options_file, err)

    for var, field in _ENV.items():
        if env.get(var, "") == "":
            continue
        raw = env[var]
        values[field] = [p.strip() for p in raw.split(",") if p.strip()] if field == "allowed_peers" else raw

    if not values.get("ha_token"):
        values["ha_token"] = env.get("SUPERVISOR_TOKEN", "")
    return Settings(**values)
