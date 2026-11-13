"""M1 acceptance: the app starts against the simulators, sits behind an ingress prefix, connects to
(fake) Home Assistant, lists areas and entities and reports connection status."""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path

import httpx
import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "agent_house" / "app" / "web" / "static"
OPENAPI = yaml.safe_load((ROOT / "contracts" / "openapi.yaml").read_text())


def wait_for(predicate, timeout=8.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


def status(base: str) -> dict:
    return httpx.get(f"{base}/api/status").json()


def connected(base: str) -> dict:
    return wait_for(lambda: (s := status(base))["ha"]["connected"] and s)


def response_schema(path: str, method: str = "get", code: str = "200") -> dict:
    schema = OPENAPI["paths"][path][method]["responses"][code]["content"]["application/json"]["schema"]
    return {**schema, "components": OPENAPI["components"]}


def assert_matches_contract(payload, path: str) -> None:
    jsonschema.validate(payload, response_schema(path), cls=jsonschema.Draft202012Validator)


# --- ingress ------------------------------------------------------------------------------


class _Urls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in ("href", "src", "action") and value:
                self.urls.append(value)


def test_gui_is_served_under_the_ingress_prefix(ingress):
    page = httpx.get(f"{ingress.base}/")
    assert page.status_code == 200 and "Agent House" in page.text
    parser = _Urls()
    parser.feed(page.text)
    assert parser.urls, "index.html references no assets?"
    for url in parser.urls:
        assert not url.startswith(("/", "http:", "https:", "//")), f"absolute URL breaks ingress: {url}"
        if not url.startswith("#"):
            assert httpx.get(f"{ingress.base}/{url}").status_code == 200, url


def test_static_sources_contain_no_absolute_urls():
    """Absolute paths and CDN links would work in `make sim` on / but 404 inside Home Assistant."""
    offenders = []
    for file in [STATIC / "index.html", STATIC / "app.js", STATIC / "app.css"]:
        text = file.read_text()
        for pattern in (
            r"""(?:href|src|action)\s*=\s*["']/""",
            r"""fetch\(\s*["'`]/""",
            r"""EventSource\(\s*["'`]/""",
            r"""url\(\s*["']?/""",
            r"""https?://(?!www\.w3\.org)""",
        ):
            offenders += [f"{file.name}: {m.group(0)}" for m in re.finditer(pattern, text)]
    assert not offenders, offenders


def test_absolute_paths_do_not_exist_outside_the_prefix(ingress):
    assert httpx.get(f"{ingress.url}/api/status").status_code == 404
    assert httpx.get(f"{ingress.url}/static/app.js").status_code == 404


def test_ingress_path_header_is_recorded(ingress):
    connected(ingress.base)
    bundle = httpx.get(f"{ingress.base}/api/diagnostics")
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(bundle.content)).read("manifest.json"))
    assert manifest["ingress"]["last_ingress_path"] == "/api/hassio_ingress/<8 chars>"
    assert b"simtoken" not in bundle.content


def test_only_the_ingress_peer_may_connect(settings):
    from app.main import create_app
    from sim.server import ServerThread

    settings.allowed_peers = ["172.30.32.2"]  # the real Supervisor address; the test client is 127.0.0.1
    with ServerThread(create_app(settings)) as server:
        for path in ("/", "/api/status", "/static/app.js", "/api/events/stream"):
            response = httpx.get(server.url + path)
            assert response.status_code == 403, path


# --- Home Assistant connection ------------------------------------------------------------


def test_status_reports_connection_and_matches_contract(ingress, ha_state):
    data = connected(ingress.base)
    assert_matches_contract(data, "/api/status")
    assert data["ha"]["version"] == ha_state.version
    assert data["ha"]["areas"] == 4
    assert data["mode"] == "shadow", "shadow mode is the default"
    assert data["kill_switch"]["engaged"] is False
    wait_for(lambda: status(ingress.base)["llm"]["state"] == "ok")
    wait_for(lambda: status(ingress.base)["jetson"]["reachable"])
    assert status(ingress.base)["jetson"]["metrics"]["power_mode"] == "MAXN_SUPER"


def test_areas_and_entities(ingress):
    connected(ingress.base)
    areas = httpx.get(f"{ingress.base}/api/areas").json()
    assert_matches_contract(areas, "/api/areas")
    assert {a["area_id"] for a in areas} == {"kitchen", "living_room", "bedroom", "hallway"}
    assert all(a["entity_count"] > 0 for a in areas)

    everything = httpx.get(f"{ingress.base}/api/entities").json()
    assert_matches_contract(everything, "/api/entities")
    by_id = {e["entity_id"]: e for e in everything}
    assert by_id["lock.front_door"]["tier"] == "T3"
    assert by_id["cover.garage_door"]["tier"] == "T3"
    assert by_id["light.hallway"]["tier"] == "T0"
    assert by_id["sensor.kitchen_temperature"]["read_only"] is True

    hallway = httpx.get(f"{ingress.base}/api/entities", params={"area": "hallway"}).json()
    assert hallway and all(e["area_id"] == "hallway" for e in hallway)
    assert sum(a["entity_count"] for a in areas) == sum(1 for e in everything if e["area_id"])
    lights = httpx.get(f"{ingress.base}/api/entities", params={"domain": "light"}).json()
    assert lights and all(e["domain"] == "light" for e in lights)
    orphans = httpx.get(f"{ingress.base}/api/entities", params={"area": "none"}).json()
    assert all(e["area_id"] is None for e in orphans)


def test_entities_inherit_the_area_of_their_device(ingress, ha_state):
    connected(ingress.base)
    via_device = [
        e["entity_id"] for e in ha_state.entity_registry if not e.get("area_id") and e.get("device_id")
    ]
    assert via_device, "fixture should contain entities that get their area from the device"
    by_id = {e["entity_id"]: e for e in httpx.get(f"{ingress.base}/api/entities").json()}
    assert all(by_id[e]["area_id"] for e in via_device if e in by_id)


def test_state_changes_reach_the_feed_and_the_stream(ingress, ha_state):
    connected(ingress.base)
    received: list[dict] = []
    with httpx.stream("GET", f"{ingress.base}/api/events/stream", timeout=10) as stream:
        lines = stream.iter_lines()
        assert next(lines).startswith(":"), "stream opens with a comment line"
        ha_state.set_state("binary_sensor.hallway_motion", "on")
        for line in lines:
            if line.startswith("data: "):
                received.append(json.loads(line[6:]))
                break
    assert received[0]["kind"] == "state"
    assert received[0]["entity_id"] == "binary_sensor.hallway_motion"

    feed = httpx.get(f"{ingress.base}/api/events", params={"kind": "state"}).json()
    assert_matches_contract(feed, "/api/events")
    assert "off → on" in feed[0]["summary"]
    states = {e["entity_id"]: e["state"] for e in httpx.get(f"{ingress.base}/api/entities").json()}
    assert states["binary_sensor.hallway_motion"] == "on"


def test_reconnects_after_home_assistant_drops_the_websocket(ingress, ha_state):
    connected(ingress.base)
    ha_state.drop_ws()
    wait_for(lambda: status(ingress.base)["ha"]["reconnects"] >= 1)
    connected(ingress.base)
    ha_state.set_state("light.hallway", "on")
    wait_for(
        lambda: (
            {e["entity_id"]: e["state"] for e in httpx.get(f"{ingress.base}/api/entities").json()}[
                "light.hallway"
            ]
            == "on"
        )
    )


def test_wrong_token_is_reported_not_retried_in_a_tight_loop(settings):
    from app.main import create_app
    from sim.server import ServerThread

    settings.ha_token = "wrong"
    with ServerThread(create_app(settings)) as server:
        data = wait_for(lambda: (s := status(server.url))["ha"]["state"] == "auth_failed" and s)
        assert data["ha"]["connected"] is False and data["ha"]["last_error"]


def test_unreachable_home_assistant_does_not_break_the_gui(settings):
    from app.main import create_app
    from sim.server import ServerThread

    settings.ha_url = "http://127.0.0.1:9"  # discard port: connection refused
    settings.jetson_llm_url = "http://127.0.0.1:9"
    with ServerThread(create_app(settings)) as server:
        data = wait_for(lambda: (s := status(server.url))["ha"]["state"] == "unreachable" and s)
        assert httpx.get(server.url + "/").status_code == 200
        assert httpx.get(server.url + "/api/areas").json() == []
        wait_for(lambda: status(server.url)["llm"]["state"] == "unreachable")
        assert data["orchestrator"]["effective"] == "none"


# --- safety switches, settings, diagnostics -----------------------------------------------


def test_mode_and_kill_switch_persist(ingress, settings):
    base = ingress.base
    assert httpx.put(f"{base}/api/kill-switch", json={"engaged": True, "reason": "test"}).json()[
        "kill_switch"
    ]["engaged"]
    assert httpx.put(f"{base}/api/mode", json={"mode": "live"}).json()["mode"] == "live"
    assert httpx.put(f"{base}/api/mode", json={"mode": "party"}).status_code == 422

    from app.store.db import Store

    store = Store(settings.data_dir / "agent_house.db")
    assert store.get("kill_switch")["engaged"] is True and store.get("kill_switch")["reason"] == "test"
    assert store.get("mode") == "live"
    kinds = [e["summary"] for e in httpx.get(f"{base}/api/events", params={"kind": "system"}).json()]
    assert any("Kill switch ENGAGED" in k for k in kinds)


def test_settings_and_diagnostics_never_leak_secrets(ingress, settings):
    settings.anthropic_api_key = "sk-ant-very-secret"
    settings.openai_api_key = "sk-proj-very-secret"
    settings.jetson_token = "jetson-very-secret"
    connected(ingress.base)
    shown = httpx.get(f"{ingress.base}/api/settings").json()
    assert_matches_contract(shown, "/api/settings")
    assert shown["options"]["ha_token"] == "***" and shown["options"]["anthropic_api_key"] == "***"
    assert shown["options"]["openai_api_key"] == "***"

    bundle = httpx.get(f"{ingress.base}/api/diagnostics")
    assert bundle.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(bundle.content))
    manifest = json.loads(archive.read("manifest.json"))
    schema = json.loads((ROOT / "contracts/schemas/diagnostics_manifest.schema.json").read_text())
    jsonschema.validate(manifest, schema)
    assert set(manifest["files"]) <= set(archive.namelist())
    blob = b"".join(archive.read(name) for name in archive.namelist())
    for secret in (settings.ha_token, "sk-ant-very-secret", "sk-proj-very-secret", "jetson-very-secret"):
        assert secret.encode() not in blob


@pytest.mark.parametrize("path", ["api/chat", "api/migration/originals"])
def test_later_milestones_answer_501_with_the_contract_error_shape(ingress, path):
    response = httpx.get(f"{ingress.base}/{path}")
    assert response.status_code == 501 and response.json()["error"] == "not_implemented"


def test_logs_endpoint(ingress):
    connected(ingress.base)
    logs = httpx.get(f"{ingress.base}/api/logs", params={"level": "info"}).json()
    assert any("connected to Home Assistant" in entry["message"] for entry in logs)


def test_entities_removed_while_disconnected_disappear_after_reconnect(ingress, ha_state):
    connected(ingress.base)
    assert any(
        e["entity_id"] == "fan.living_room_fan" for e in httpx.get(f"{ingress.base}/api/entities").json()
    )
    before = status(ingress.base)["ha"]["reconnects"]
    ha_state.states.pop("fan.living_room_fan")  # removed in HA without us seeing an event
    ha_state.drop_ws()
    wait_for(lambda: status(ingress.base)["ha"]["reconnects"] > before)
    connected(ingress.base)
    assert all(
        e["entity_id"] != "fan.living_room_fan" for e in httpx.get(f"{ingress.base}/api/entities").json()
    )


def test_reconnect_after_a_working_session_is_quick(ingress, ha_state):
    connected(ingress.base)
    for _ in range(4):  # with a backoff that never resets, the 4th reconnect would wait 8 s
        before = status(ingress.base)["ha"]["reconnects"]
        ha_state.drop_ws()
        wait_for(lambda b=before: status(ingress.base)["ha"]["reconnects"] > b, timeout=4)
        wait_for(lambda: status(ingress.base)["ha"]["connected"], timeout=4)
