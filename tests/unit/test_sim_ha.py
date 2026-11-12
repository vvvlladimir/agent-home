import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import websockets

from sim.fake_ha import HAState, create_app
from sim.server import ServerThread

H = {"Authorization": "Bearer sim-token"}


@pytest.fixture
def ha():
    state = HAState.default()
    with ServerThread(create_app(state)) as srv:
        srv.state = state
        yield srv


@pytest.fixture
def api(ha):
    with httpx.Client(base_url=ha.url, headers=H) as c:
        yield c


def test_auth_and_prefixes(ha, api):
    assert httpx.get(f"{ha.url}/api/states").status_code == 401
    assert httpx.get(f"{ha.url}/api/states").json()["message"]
    assert api.get("/api/").json() == {"message": "API running."}
    assert api.get("/core/api/config").json()["version"] == "2026.11.1"
    assert len(api.get("/core/api/states").json()) >= 26
    assert api.get("/api/states/light.nope").status_code == 404


def test_services(ha, api):
    r = api.post("/api/services/light/turn_on", json={"entity_id": ["light.hallway"], "brightness_pct": 50})
    assert r.json()[0]["state"] == "on" and r.json()[0]["attributes"]["brightness"] == 128
    api.post("/api/services/light/toggle", json={"entity_id": "light.hallway"})
    assert api.get("/api/states/light.hallway").json()["state"] == "off"
    api.post(
        "/api/services/climate/set_temperature", json={"entity_id": "climate.bedroom", "temperature": 22}
    )
    assert api.get("/api/states/climate.bedroom").json()["attributes"]["temperature"] == 22
    api.post(
        "/api/services/cover/set_cover_position", json={"entity_id": "cover.bedroom_blinds", "position": 40}
    )
    assert api.get("/api/states/cover.bedroom_blinds").json()["state"] == "open"
    api.post("/api/services/lock/unlock", json={"entity_id": "lock.front_door"})
    assert api.get("/api/states/lock.front_door").json()["state"] == "unlocked"
    api.post("/api/services/automation/trigger", json={"entity_id": "automation.evening_blinds"})
    assert api.get("/api/states/automation.evening_blinds").json()["attributes"]["last_triggered"]
    r = api.post("/api/services/foo/bar", json={})
    assert r.status_code == 400 and r.json() == {"message": "Service not found."}
    assert [c["service"] for c in ha.state.service_calls][:2] == ["turn_on", "toggle"]


def test_automation_crud(ha, api):
    assert api.get("/api/config/automation/config/1700000000001").json()["alias"] == "Hallway motion light"
    assert api.get("/api/config/automation/config/zzz").json() == {"message": "Resource not found"}
    bad = api.post(
        "/api/config/automation/config/x", json={"alias": "A", "trigger": [], "action": [], "bogus": 1}
    )
    assert bad.status_code == 400 and "data['bogus']" in bad.json()["message"]
    assert api.post("/api/config/automation/config/x", json={"alias": "A"}).status_code == 400
    ok = api.post(
        "/api/config/automation/config/agent_house_1",
        json={"alias": "Agent House: Test!", "triggers": [{"trigger": "sun"}], "actions": [{"delay": 1}]},
    )
    assert ok.json() == {"result": "ok"}
    st = api.get("/api/states/automation.agent_house_test").json()
    assert st["state"] == "on" and st["attributes"]["id"] == "agent_house_1"
    assert ha.state.config_writes[-1]["id"] == "agent_house_1"
    assert api.delete("/api/config/automation/config/agent_house_1").json() == {"result": "ok"}
    assert api.get("/api/states/automation.agent_house_test").status_code == 404
    assert api.post("/api/config/core/check_config").json()["result"] == "valid"


def test_history_filter_and_fail_next(ha, api):
    t0 = datetime.now(UTC) - timedelta(hours=3)
    ha.state.set_state("sensor.living_room_temperature", "19", ts=t0)
    ha.state.set_state("sensor.living_room_temperature", "20", ts=t0 + timedelta(hours=1))
    ha.state.set_state("sensor.bedroom_temperature", "18", ts=t0)
    start = (t0 - timedelta(minutes=1)).isoformat()
    r = api.get(
        f"/api/history/period/{start.replace('+', '%2B')}",
        params={
            "filter_entity_id": "sensor.living_room_temperature,sensor.bedroom_temperature",
            "end_time": (t0 + timedelta(minutes=30)).isoformat(),
        },
    )
    data = r.json()
    assert [[s["state"] for s in lst] for lst in data] == [["19"], ["18"]]
    assert api.get("/api/history/period", params={"filter_entity_id": "light.hallway"}).status_code == 200
    ha.state.fail_next(1, 503)
    assert api.get("/api/states").status_code == 503
    assert api.get("/api/states").status_code == 200


async def _ws(ha, path="/api/websocket"):
    ws = await websockets.connect(ha.ws_url + path)
    assert json.loads(await ws.recv())["type"] == "auth_required"
    await ws.send(json.dumps({"type": "auth", "access_token": "sim-token"}))
    assert json.loads(await ws.recv())["type"] == "auth_ok"
    return ws


async def _cmd(ws, **kw):
    await ws.send(json.dumps(kw))
    return json.loads(await asyncio.wait_for(ws.recv(), 5))


async def test_ws_auth_invalid(ha):
    ws = await websockets.connect(ha.ws_url + "/core/websocket")
    await ws.recv()
    await ws.send(json.dumps({"type": "auth", "access_token": "nope"}))
    assert json.loads(await ws.recv())["type"] == "auth_invalid"
    with pytest.raises(websockets.ConnectionClosed):
        await ws.recv()


async def test_ws_commands_and_events(ha):
    ws = await _ws(ha, "/core/websocket")
    assert (await _cmd(ws, id=1, type="ping"))["type"] == "pong"
    areas = (await _cmd(ws, id=2, type="config/area_registry/list"))["result"]
    assert {a["area_id"] for a in areas} == {"kitchen", "living_room", "bedroom", "hallway"}
    ents = (await _cmd(ws, id=3, type="config/entity_registry/list"))["result"]
    assert any(e["area_id"] is None and e["device_id"] for e in ents)
    devs = (await _cmd(ws, id=4, type="config/device_registry/list"))["result"]
    assert all(d["area_id"] for d in devs)
    assert len((await _cmd(ws, id=5, type="get_states"))["result"]) >= 26
    v = (await _cmd(ws, id=6, type="validate_config", trigger=[{"platform": "state"}], action=[]))["result"]
    assert v["trigger"]["valid"] and not v["action"]["valid"]
    assert (await _cmd(ws, id=7, type="bogus"))["error"]["code"] == "unknown_command"
    assert (await _cmd(ws, id=8, type="subscribe_events", event_type="state_changed"))["success"]
    ha.state.set_state("binary_sensor.hallway_motion", "on")  # from the test thread
    ev = json.loads(await asyncio.wait_for(ws.recv(), 5))
    assert ev["id"] == 8 and ev["event"]["data"]["new_state"]["state"] == "on"
    assert ev["event"]["data"]["old_state"]["state"] == "off"
    r = await _cmd(
        ws,
        id=9,
        type="call_service",
        domain="light",
        service="turn_on",
        service_data={},
        target={"entity_id": "light.hallway"},
    )
    assert r["success"]
    assert json.loads(await asyncio.wait_for(ws.recv(), 5))["event"]["data"]["entity_id"] == "light.hallway"
    r = await _cmd(ws, id=10, type="call_service", domain="x", service="y")
    assert r["error"]["code"] == "service_not_found"
    await ws.close()


async def test_ws_drop(ha):
    ws = await _ws(ha)
    ha.state.drop_ws()
    with pytest.raises(websockets.ConnectionClosed):
        await asyncio.wait_for(ws.recv(), 5)
    ws2 = await _ws(ha)  # reconnect works
    await ws2.close()
