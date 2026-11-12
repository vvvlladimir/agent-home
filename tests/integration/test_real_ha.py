"""Opt-in (`make test-realha`): run the HA client against a real Home Assistant Core.

Purpose: catch API-shape mistakes that the hand-written simulator would happily accept.
The probes at the bottom pin down the shapes M3/M4 will rely on (service calls, automation
config endpoints, validate_config) so `sim/fake_ha.py` can be kept honest.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import Settings
from app.ha.client import HAClient

from . import real_ha

pytestmark = pytest.mark.realha


@pytest.fixture(scope="module")
def ha(tmp_path_factory):
    if not real_ha.hass_binary():
        pytest.skip("no hass binary: pip install homeassistant or set AH_HASS_BIN")
    instance = real_ha.start(tmp_path_factory.mktemp("hass"))
    yield instance
    instance.stop()


@pytest.fixture
def rest(ha):
    with httpx.Client(base_url=ha.url, headers={"Authorization": f"Bearer {ha.token}"}, timeout=20) as client:
        yield client


async def _client(ha, tmp_path) -> HAClient:
    client = HAClient(Settings(data_dir=tmp_path, ha_url=ha.url, ha_token=ha.token))
    await client.start()
    assert await client.wait_ready(30), client.last_error
    return client


async def test_client_connects_and_maps_areas(ha, tmp_path):
    client = await _client(ha, tmp_path)
    try:
        assert client.connected and client.version
        assert "input_boolean.hallway_motion" in client.states

        area = await client.command("config/area_registry/create", name="Hallway")
        assert {"area_id", "name"} <= set(area)
        updated = await client.command(
            "config/entity_registry/update", entity_id="input_boolean.hallway_light", area_id=area["area_id"]
        )
        assert updated["entity_entry"]["area_id"] == area["area_id"]

        entries = await client.command("config/entity_registry/list")
        assert {"entity_id", "area_id", "device_id", "disabled_by", "hidden_by", "name", "original_name"} <= set(
            entries[0]
        )
        assert isinstance(await client.command("config/device_registry/list"), list)

        await client.refresh_registry()
        assert client.registry.area_of("input_boolean.hallway_light") == area["area_id"]
        assert client.registry.entities_in(area["area_id"]) == ["input_boolean.hallway_light"]
    finally:
        await client.stop()


async def test_state_changed_events_arrive(ha, tmp_path, rest):
    client = await _client(ha, tmp_path)
    seen: list[tuple] = []
    client.on_state_changed(lambda entity_id, old, new: seen.append((entity_id, old["state"], new["state"])))
    try:
        rest.post("/api/services/input_boolean/turn_off", json={"entity_id": "input_boolean.hallway_motion"})
        await asyncio.sleep(0.5)
        seen.clear()
        changed = rest.post(
            "/api/services/input_boolean/turn_on", json={"entity_id": "input_boolean.hallway_motion"}
        )
        assert changed.status_code == 200 and isinstance(changed.json(), list)
        for _ in range(50):
            if ("input_boolean.hallway_motion", "off", "on") in seen:
                break
            await asyncio.sleep(0.1)
        assert ("input_boolean.hallway_motion", "off", "on") in seen
        # the owner's automation reacted: proof that the fixture automation really runs
        assert client.states["input_boolean.hallway_light"]["state"] == "on"
    finally:
        await client.stop()


def test_wrong_token_is_rejected(ha):
    assert httpx.get(f"{ha.url}/api/states", headers={"Authorization": "Bearer nope"}).status_code == 401


# --- shape probes for M3/M4 ---------------------------------------------------------------


def test_probe_automation_entity_exposes_its_config_id(rest):
    state = rest.get("/api/states/automation.hallway_motion_light").json()
    assert state["state"] == "on"
    assert state["attributes"]["id"] == "1700000000001"
    assert "last_triggered" in state["attributes"]


def test_probe_automation_config_roundtrip(rest):
    path = "/api/config/automation/config/agent_house_probe"
    config = {
        "alias": "Agent House probe",
        "description": "created by tests",
        "triggers": [{"trigger": "state", "entity_id": "input_boolean.hallway_motion", "to": "off"}],
        "conditions": [],
        "actions": [{"action": "input_boolean.turn_off", "target": {"entity_id": "input_boolean.hallway_light"}}],
        "mode": "single",
    }
    created = rest.post(path, json=config)
    assert created.status_code == 200 and created.json() == {"result": "ok"}
    stored = rest.get(path).json()
    assert stored["alias"] == "Agent House probe" and stored["id"] == "agent_house_probe"

    for _ in range(50):  # the POST reloads automations by itself
        state = rest.get("/api/states/automation.agent_house_probe")
        if state.status_code == 200:
            break
        asyncio.run(asyncio.sleep(0.1))
    assert state.status_code == 200 and state.json()["attributes"]["id"] == "agent_house_probe"

    off = rest.post("/api/services/automation/turn_off", json={"entity_id": "automation.agent_house_probe"})
    assert off.status_code == 200
    assert rest.get("/api/states/automation.agent_house_probe").json()["state"] == "off"

    bad = rest.post(path, json={"alias": "broken", "bogus_key": 1})
    assert bad.status_code == 400 and "message" in bad.json()

    assert rest.delete(path).json() == {"result": "ok"}
    assert rest.get(path).status_code == 404
    # the owner's own automation is untouched
    assert rest.get("/api/config/automation/config/1700000000001").status_code == 200


def test_probe_check_config(rest):
    result = rest.post("/api/config/core/check_config").json()
    assert result["result"] == "valid"


async def test_probe_validate_config_over_websocket(ha, tmp_path):
    client = await _client(ha, tmp_path)
    try:
        good = await client.command(
            "validate_config",
            triggers=[{"trigger": "state", "entity_id": "input_boolean.hallway_motion"}],
            actions=[{"action": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.hallway_light"}}],
        )
        assert good["triggers"]["valid"] is True and good["actions"]["valid"] is True
        bad = await client.command("validate_config", triggers=[{"trigger": "no_such_platform"}])
        assert bad["triggers"]["valid"] is False and bad["triggers"]["error"]
    finally:
        await client.stop()


def test_probe_history(rest):
    history = rest.get(
        "/api/history/period", params={"filter_entity_id": "input_boolean.hallway_motion"}
    ).json()
    assert isinstance(history, list)
    assert history == [] or history[0][0]["entity_id"] == "input_boolean.hallway_motion"
