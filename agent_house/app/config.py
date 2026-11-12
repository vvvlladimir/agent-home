"""Effective configuration.

Precedence (lowest → highest): built-in defaults → add-on options (`/data/options.json`,
written by the Supervisor from the HA Configuration tab) → `AH_*` environment variables
(used by the simulator and tests).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

SUPERVISOR_INGRESS_IP = "172.30.32.2"  # the only peer HA ingress connects from
SECRET_FIELDS = ("ha_token", "anthropic_api_key", "jetson_token")


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
    anthropic_model: str = "claude-opus-5-5"
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
        return data


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
