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
        json.dumps({"log_level": "debug", "daily_budget_eur": 2.5, "jetson_learner_url": "", "unknown": 1})
    )
    s = load_settings({"AH_DATA_DIR": str(tmp_path)})
    assert s.log_level == "debug" and s.daily_budget_eur == 2.5 and s.jetson_learner_url == ""
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


def test_an_install_from_before_0_5_0_starts_and_keeps_its_settings(tmp_path):
    """D40: the room agents' and the local model's options and settings are ignored, not fatal."""
    import yaml

    from app.config import load_runtime

    (tmp_path / "options.json").write_text(json.dumps({
        "jetson_llm_url": "http://10.0.0.5:8080", "jetson_metrics_url": "http://10.0.0.5:9100",
        "jetson_learner_url": "http://10.0.0.5:9200", "orchestrator_provider": "local",
    }))  # fmt: skip
    s = load_settings({"AH_DATA_DIR": str(tmp_path)})
    assert s.jetson_learner_url == "http://10.0.0.5:9200" and not hasattr(s, "jetson_llm_url")
    assert "jetson_llm_url" not in s.masked()
    (tmp_path / "settings.yaml").write_text(yaml.safe_dump({
        "llm_timeout_s": 300, "llm_max_retries": 2, "llm_stale_after_s": 900, "llm_max_queue": 16,
        "llm_presence_penalty": 0, "learner_mode": "live", "manager_enabled": True, "habit_days": 21,
    }))  # fmt: skip
    runtime = load_runtime(tmp_path)
    assert runtime.learner_mode == "live" and runtime.manager_enabled and runtime.habit_days == 21
    assert not any(k.startswith("llm_") for k in runtime.model_dump())
