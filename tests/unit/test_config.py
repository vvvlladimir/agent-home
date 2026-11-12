"""Settings: precedence and redaction."""

from __future__ import annotations

import json

import pytest

from app.config import load_settings, redact_url
from app.ha.client import HAClient, HAError


def test_defaults_target_the_supervisor_proxy():
    s = load_settings({"AH_DATA_DIR": "/nonexistent", "SUPERVISOR_TOKEN": "tok"})
    assert s.ha_rest_url == "http://supervisor/core/api"
    assert s.ha_websocket_url == "ws://supervisor/core/websocket"
    assert s.ha_token == "tok" and s.via_supervisor
    assert s.allowed_peers == ["172.30.32.2"]


def test_plain_core_url_uses_the_api_websocket_path():
    s = load_settings({"AH_DATA_DIR": "/nonexistent", "AH_HA_URL": "https://ha.example:8123/"})
    assert s.ha_websocket_url == "wss://ha.example:8123/api/websocket"
    assert not s.via_supervisor


def test_options_file_then_env_precedence(tmp_path):
    (tmp_path / "options.json").write_text(
        json.dumps({"log_level": "debug", "daily_budget_eur": 2.5, "jetson_llm_url": "", "unknown": 1})
    )
    s = load_settings({"AH_DATA_DIR": str(tmp_path)})
    assert s.log_level == "debug" and s.daily_budget_eur == 2.5 and s.jetson_llm_url == ""
    s = load_settings({"AH_DATA_DIR": str(tmp_path), "AH_LOG_LEVEL": "error", "AH_ALLOWED_PEERS": "a, b"})
    assert s.log_level == "error" and s.allowed_peers == ["a", "b"]


def test_a_broken_options_file_does_not_stop_the_app(tmp_path):
    (tmp_path / "options.json").write_text("{not json")
    assert load_settings({"AH_DATA_DIR": str(tmp_path)}).log_level == "info"


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("http://192.168.1.50:8080", "http://192.168.1.50:8080"),
        ("http://user:pw@jetson:8080/v1", "http://***@jetson:8080/v1"),
        ("https://x.example/path?token=abc", "https://x.example/path?***"),
        ("", ""),
    ],
)
def test_urls_are_shown_without_credentials(url, shown):
    assert redact_url(url) == shown
    assert "pw" not in redact_url(url) and "abc" not in redact_url(url)


async def test_ha_client_refuses_commands_that_are_not_read_only(tmp_path):
    client = HAClient(load_settings({"AH_DATA_DIR": str(tmp_path)}))
    for command in ("call_service", "config/entity_registry/remove", "fire_event"):
        with pytest.raises(HAError, match="read-only allowlist"):
            await client.command(command)
    await client.stop()
