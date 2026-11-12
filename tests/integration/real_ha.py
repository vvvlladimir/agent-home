"""Start a real, headless Home Assistant Core for `make test-realha`.

No onboarding and no browser: the auth store is pre-seeded with an owner and a long-lived
token whose JWT we sign ourselves (HS256 with the refresh token's `jwt_key`, `iss` = token id).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

CONFIG = """
homeassistant:
  name: Real HA under test
  latitude: 0
  longitude: 0
  elevation: 0
  unit_system: metric
  time_zone: UTC
  country: DE
http:
  server_host: 127.0.0.1
  server_port: {port}
api:
websocket_api:
config:
history:
recorder:
automation: !include automations.yaml
input_boolean:
  hallway_motion:
    name: Hallway motion
  hallway_light:
    name: Hallway light
"""

AUTOMATIONS = """
- id: "1700000000001"
  alias: Hallway motion light
  triggers:
    - trigger: state
      entity_id: input_boolean.hallway_motion
      to: "on"
  actions:
    - action: input_boolean.turn_on
      target:
        entity_id: input_boolean.hallway_light
"""


def hass_binary() -> str | None:
    return os.environ.get("AH_HASS_BIN") or shutil.which("hass")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwt(refresh_id: str, jwt_key: str) -> str:
    now = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({"iss": refresh_id, "iat": now, "exp": now + 3600 * 24 * 365}).encode())
    signature = hmac.new(jwt_key.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class RealHA:
    url: str
    token: str
    process: subprocess.Popen
    log: Path

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(20)
        except subprocess.TimeoutExpired:
            self.process.kill()


def start(config_dir: Path, timeout: float = 240.0) -> RealHA:
    binary = hass_binary()
    if not binary:
        raise RuntimeError("no hass binary (pip install homeassistant, or set AH_HASS_BIN)")
    port = _free_port()
    user_id, refresh_id = uuid.uuid4().hex, uuid.uuid4().hex
    jwt_key = uuid.uuid4().hex + uuid.uuid4().hex
    storage = config_dir / ".storage"
    storage.mkdir(parents=True)
    (config_dir / "configuration.yaml").write_text(CONFIG.format(port=port))
    (config_dir / "automations.yaml").write_text(AUTOMATIONS)
    (storage / "onboarding").write_text(
        json.dumps(
            {
                "version": 4,
                "minor_version": 1,
                "key": "onboarding",
                "data": {"done": ["user", "core_config", "analytics", "integration"]},
            }
        )
    )
    groups = [
        {"id": "system-admin", "name": "Administrators"},
        {"id": "system-users", "name": "Users"},
        {"id": "system-read-only", "name": "Read Only"},
    ]
    (storage / "auth").write_text(
        json.dumps(
            {
                "version": 1,
                "minor_version": 1,
                "key": "auth",
                "data": {
                    "users": [
                        {
                            "id": user_id,
                            "group_ids": ["system-admin"],
                            "is_owner": True,
                            "is_active": True,
                            "name": "CI",
                            "system_generated": False,
                            "local_only": False,
                        }
                    ],
                    "groups": groups,
                    "credentials": [],
                    "refresh_tokens": [
                        {
                            "id": refresh_id,
                            "user_id": user_id,
                            "client_id": None,
                            "client_name": "agent-house-ci",
                            "client_icon": None,
                            "token_type": "long_lived_access_token",
                            "created_at": "2026-01-01T00:00:00+00:00",
                            "access_token_expiration": 315360000.0,
                            "token": uuid.uuid4().hex + uuid.uuid4().hex,
                            "jwt_key": jwt_key,
                            "last_used_at": None,
                            "last_used_ip": None,
                            "expire_at": None,
                            "credential_id": None,
                            "version": None,
                        }
                    ],
                },
            }
        )
    )
    log = config_dir / "hass.log"
    process = subprocess.Popen(  # noqa: S603
        [binary, "-c", str(config_dir), "--log-file", str(log)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ha = RealHA(url=f"http://127.0.0.1:{port}", token=_jwt(refresh_id, jwt_key), process=process, log=log)
    deadline = time.time() + timeout
    headers = {"Authorization": f"Bearer {ha.token}"}
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"hass exited with {process.returncode}; see {log}")
        try:
            config = httpx.get(f"{ha.url}/api/config", headers=headers, timeout=3)
            if config.status_code == 200 and config.json().get("state") == "RUNNING":
                return ha
            if config.status_code == 401:
                ha.stop()
                raise RuntimeError("hass rejected the pre-seeded token (auth store format changed?)")
        except httpx.HTTPError:
            pass
        time.sleep(1)
    ha.stop()
    raise RuntimeError(f"hass did not reach RUNNING within {timeout:.0f}s; see {log}")
