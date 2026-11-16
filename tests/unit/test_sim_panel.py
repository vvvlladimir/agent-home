"""The sim panel's machinery: house generator, life, scenarios, config/secrets, model gateways, control API.

Hermetic: the "real" providers are the fakes on ephemeral ports.
"""

from __future__ import annotations

import json
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from sim import control, fake_anthropic, fake_ha, fake_openai, house_gen
from sim.gateways import Gateways
from sim.life import Life
from sim.scenarios import SCENARIOS
from sim.server import ServerThread
from sim.sim_config import SimConfig

BASE = len(fake_ha.HAState.default().states)


def built(preset: str = "realistic", **kw) -> tuple[fake_ha.HAState, dict]:
    ha = fake_ha.HAState.default()
    _, streams = house_gen.build(ha, house_gen.HouseSpec.from_preset(preset, **kw))
    return ha, streams


def recorder(ha: fake_ha.HAState) -> list[dict]:
    seen: list[dict] = []
    orig = ha.fire_event

    def fire(event_type, data=None, context=None):
        if not ha.quiet:
            seen.append({"type": event_type, **(data or {})})
        orig(event_type, data, context)

    ha.fire_event = fire
    return seen


# ---- house generator ----------------------------------------------------------------------------
def test_presets_grow_a_messy_but_consistent_house():
    ha, streams = built("realistic")
    s = house_gen.summary(ha)
    assert 250 < s["entities"] < 500 and s["devices"] > 50
    assert s["unavailable"] > 0 and s["unknown"] > 0 and s["disabled"] > 0 and s["no_area"] > 0
    reg = {e["entity_id"]: e for e in ha.entity_registry}
    assert set(ha.states) <= set(reg) | {e for e in ha.states if e.startswith("automation.")}
    assert all(e not in ha.states for e, r in reg.items() if r["disabled_by"])  # disabled = registry only
    platforms = {r["platform"] for r in reg.values()}
    assert {"mqtt", "fritz", "hassio"} <= platforms
    assert any(e.split(".")[1].startswith("0x") for e in ha.states)  # unrenamed Zigbee devices
    assert "binary_sensor.zigbee2mqtt_bridge_connection_state" in ha.states
    assert sum(e.startswith("device_tracker.") for e in ha.states) == 30
    assert streams and all(e in ha.states for e in streams)
    large, _ = built("large")
    assert len(large.states) > 1000


def test_generator_is_deterministic_and_rebuild_returns_to_the_base_house():
    a, _ = built("realistic", seed=7)
    b, _ = built("realistic", seed=7)
    assert sorted(a.states) == sorted(b.states)
    assert sorted(a.states) != sorted(built("realistic", seed=8)[0].states)
    house_gen.build(a, house_gen.HouseSpec.from_preset("minimal"))
    assert len(a.states) == BASE and not a.generated
    assert all(not e.get("sim_generated") for e in a.entity_registry + a.devices + a.areas)


def test_rebuild_tells_the_add_on_like_an_integration_reload():
    ha, _ = built("realistic")
    seen = recorder(ha)
    house_gen.build(ha, house_gen.HouseSpec.from_preset("minimal"))
    removed = [e for e in seen if e["type"] == "state_changed" and e["new_state"] is None]
    assert len(removed) > 250
    assert [e["type"] for e in seen[-3:]] == [
        "area_registry_updated", "device_registry_updated", "entity_registry_updated",
    ]  # fmt: skip


def test_spec_validates_and_clamps():
    with pytest.raises(ValueError):
        house_gen.HouseSpec.from_preset("mansion")
    s = house_gen.HouseSpec(zigbee_devices=10_000, junk_percent=-5).clamp()
    assert s.zigbee_devices == 400 and s.junk_percent == 0


# ---- life ---------------------------------------------------------------------------------------
def test_streams_move_and_pause():
    ha, streams = built("realistic")
    life = Life(ha, seed=1)
    life.set_streams(streams)
    before = {e: ha.states[e]["state"] for e in life.streams}
    life.rate = 0
    life.tick(time.monotonic() + 10_000)
    assert {e: ha.states[e]["state"] for e in life.streams} == before
    life.rate = 1
    life.tick(time.monotonic() + 10_000)
    assert sum(ha.states[e]["state"] != v for e, v in before.items()) > 20
    assert life.stats()["events_last_min"] > 20


def test_owner_automations_run_with_an_automation_cause_unless_disabled():
    ha = fake_ha.HAState.default()
    life = Life(ha)
    ha.physical_change("binary_sensor.hallway_motion", "on")
    assert ha.states["light.hallway"]["state"] == "on"
    row = next(r for r in reversed(ha.logbook_rows) if r["entity_id"] == "light.hallway")
    assert row["context_event_type"] == "automation_triggered"
    assert row["context_entity_id"] == "automation.hallway_motion_light"
    assert life.events[-1]["cause"] == "automation" and life.automation_runs == 1
    ha.physical_change("binary_sensor.hallway_motion", "off")
    ha.call_service("light", "turn_off", {"entity_id": "light.hallway"})
    ha.set_state("automation.hallway_motion_light", "off")  # the migration's reversible disable
    ha.physical_change("binary_sensor.hallway_motion", "on")
    assert ha.states["light.hallway"]["state"] == "off"


def test_backfill_writes_quiet_history_and_keeps_the_live_state():
    ha, streams = built("realistic")
    life = Life(ha)
    life.set_streams(streams)
    live = {e: s["state"] for e, s in ha.states.items()}
    seen = recorder(ha)
    result = life.backfill(14, owner_tracker="device_tracker.iphone")
    assert result["changes"] > 1000 and not seen
    assert {e: s["state"] for e, s in ha.states.items()} == live
    now = datetime.now(UTC)
    rows = ha.history_for(["light.kitchen_ceiling"], now - timedelta(days=14), now)[0]
    assert len([r for r in rows if r["state"] == "on"]) >= 12
    assert [r["last_updated"] for r in rows] == sorted(r["last_updated"] for r in rows)
    user = [r for r in ha.logbook_for(now - timedelta(days=14), now) if r.get("context_user_id")]
    assert len(user) > 100
    assert any(r.get("context_event_type") == "automation_triggered" for r in ha.logbook_rows)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenarios_only_touch_entities_that_exist(name):
    ha, _ = built("realistic")
    steps = SCENARIOS[name][1](ha)
    assert steps, name
    assert all("wait" in s or s["entity"] in ha.states for s in steps)
    assert all(s.get("cause", "physical") in ("user", "physical", "automation") for s in steps)


def test_scenario_steps_apply_with_their_cause():
    ha = fake_ha.HAState.default()
    life = Life(ha)
    for st in SCENARIOS["night"][1](ha):
        if "wait" not in st:
            life.apply(st)
    assert ha.states["alarm_control_panel.home"]["state"] == "armed_home"
    assert all(ha.states[e]["state"] == "off" for e in ha.states if e.startswith("light."))


# ---- config + secrets -----------------------------------------------------------------------------
def test_config_updates_are_atomic_and_secrets_stay_private(tmp_path):
    cfg = SimConfig.load(tmp_path)
    with pytest.raises(ValueError):
        cfg.update({"jetson": {"learner": "mainframe", "host": "192.168.1.5"}})
    assert cfg.jetson.learner == "sim" and cfg.jetson.host == ""
    with pytest.raises(ValueError):
        cfg.update({"jetson": {"host": "http://192.168.1.5:8080"}})
    with pytest.raises(ValueError, match="unknown setting room"):
        cfg.update({"room": {"backend": "jetson"}})  # room agents are gone (D40)
    cfg.update({"jetson": {"learner": "jetson", "host": "192.168.1.5"}})
    assert cfg.jetson.learner_url() == "http://192.168.1.5:9200"
    cfg.set_secret("openai_api_key", "sk-proj-abcdefgh12345678")
    cfg.save()
    assert stat.S_IMODE((tmp_path / "secrets.json").stat().st_mode) == 0o600
    assert "sk-proj" not in (tmp_path / "config.json").read_text()
    assert "sk-proj" not in json.dumps(cfg.public()) and cfg.public()["secrets"]["openai_api_key"] == "…5678"
    again = SimConfig.load(tmp_path)
    assert again.jetson.learner == "jetson" and again.secret("openai_api_key") == "sk-proj-abcdefgh12345678"
    with pytest.raises(ValueError):
        cfg.set_secret("compatible_api_key", "x")


def test_a_panel_config_from_before_d40_still_loads(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "room": {"backend": "jetson", "pace": True}, "jetson": {"host": "10.0.0.7", "llm_port": 8080,
        "metrics_port": 9100, "metrics": "auto"}, "anthropic": {"backend": "real"}, "backfill_days": 3,
    }))  # fmt: skip
    cfg = SimConfig.load(tmp_path)
    assert cfg.jetson.host == "10.0.0.7" and cfg.anthropic.backend == "real" and cfg.backfill_days == 3


# ---- gateways ------------------------------------------------------------------------------------
@pytest.fixture
def gw_world(tmp_path):
    """Fakes as both the 'internal' servers and the 'real' upstream providers, gateways in front;
    the learner is the real jetson/learner in this process."""
    import sys
    import threading

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "jetson"))
    from learner.server import Learner
    from learner.server import serve as serve_learner

    learner = Learner(tmp_path / "learner", follow_time_zone=False)
    learner_http = serve_learner(learner, "127.0.0.1", 0, "jetson-sim")
    threading.Thread(target=learner_http.serve_forever, daemon=True).start()
    real_claude, real_openai = fake_anthropic.AnthropicSim(), fake_openai.OpenAISim()
    real_claude.api_key, real_openai.api_key = "sk-ant-REAL", "sk-proj-REAL"
    servers = {
        "anthropic": ServerThread(fake_anthropic.create_app()),
        "openai": ServerThread(fake_openai.create_app()),
        "real_claude": ServerThread(fake_anthropic.create_app(real_claude)),
        "real_openai": ServerThread(fake_openai.create_app(real_openai)),
    }
    for s in servers.values():
        s.start()
    cfg = SimConfig.load(tmp_path)
    internal = {k: servers[k].url for k in ("anthropic", "openai")}
    internal["learner"] = f"http://127.0.0.1:{learner_http.server_address[1]}"
    gw = Gateways(cfg, internal,
                  upstream={"anthropic": servers["real_claude"].url, "openai": servers["real_openai"].url})  # fmt: skip
    fronts = {
        "learner": ServerThread(gw.learner_app()), "anthropic": ServerThread(gw.cloud_app("anthropic")),
        "openai": ServerThread(gw.cloud_app("openai")),
    }  # fmt: skip
    for s in fronts.values():
        s.start()
    yield {"cfg": cfg, "gw": gw, "fronts": fronts, "claude": real_claude, "openai": real_openai, "learner": learner}
    for s in [*fronts.values(), *servers.values()]:
        s.stop()
    learner_http.shutdown()
    learner.journal.close()


def test_the_learner_gateway_serves_the_learner_faults_and_off(gw_world):
    cfg, url = gw_world["cfg"], gw_world["fronts"]["learner"].url
    auth = {"Authorization": "Bearer jetson-sim"}
    assert httpx.get(f"{url}/status").status_code == 401, "the learner's own token check"
    assert httpx.get(f"{url}/status", headers=auth).json()["journal"]["events"] == 0
    events = {"schema_version": 1, "events": [{"ts": 1.7e9, "kind": "state", "entity_id": "light.a",
                                               "state": "on", "cause": "user", "ref": None}]}  # fmt: skip
    assert httpx.post(f"{url}/events", json=events, headers=auth).json()["accepted"] == 1
    cfg.faults["learner"].fail_next = 1
    assert httpx.get(f"{url}/model", headers=auth).status_code == 500
    assert httpx.get(f"{url}/model", headers=auth).status_code == 404, "not trained yet"
    cfg.update({"jetson": {"learner": "off"}})
    assert httpx.get(f"{url}/status", headers=auth).status_code == 503, "down: the add-on falls back"
    cfg.update({"jetson": {"learner": "jetson"}})
    assert httpx.get(f"{url}/status", headers=auth).status_code == 503, "no Jetson address yet"


def test_orchestrator_passthrough_swaps_keys_and_overrides_the_model(gw_world):
    cfg, url = gw_world["cfg"], gw_world["fronts"]["anthropic"].url
    req = {"model": "claude-haiku-5-5", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
    head = {"x-api-key": "sk-ant-sim", "anthropic-version": "2023-06-01"}
    assert httpx.post(f"{url}/v1/messages", json=req, headers=head).status_code == 200  # the fake
    assert not gw_world["claude"].requests
    cfg.update({"anthropic": {"backend": "real"}})
    assert httpx.post(f"{url}/v1/messages", json=req, headers=head).status_code == 401  # no key yet
    cfg.set_secret("anthropic_api_key", "sk-ant-REAL")
    cfg.update({"anthropic": {"model_override": "claude-sonnet-5-5"}})
    r = httpx.post(f"{url}/v1/messages", json=req, headers=head)
    assert r.status_code == 200 and r.json()["model"] == "claude-sonnet-5-5"
    seen = gw_world["claude"].requests[-1]["headers"]
    assert seen["x-api-key"] == "sk-ant-REAL" and seen["anthropic-version"] == "2023-06-01"
    assert "sk-ant-REAL" not in json.dumps(gw_world["gw"].log.list())
    gw_world["cfg"].faults["anthropic"].fail_next = 1
    assert httpx.post(f"{url}/v1/messages", json=req, headers=head).status_code == 500


# ---- control API ----------------------------------------------------------------------------------
@pytest.fixture
def panel(tmp_path):
    ha = fake_ha.HAState.default()
    cfg = SimConfig.load(tmp_path)
    sim = control.Sim(ha, None, cfg, Gateways(cfg, {}))
    sim.life = Life(ha)
    sim.rebuild(house_gen.HouseSpec.from_preset("minimal"), 0)
    with ServerThread(control.create_app(sim, 0)) as server:
        app = control.create_app(sim, server.port)  # the guard needs the real port
        server.stop()
        server.app = app
        server.start()
        yield server, sim


def test_panel_guards_against_other_sites(panel):
    server, _ = panel
    assert httpx.post(f"{server.url}/api/flow", json={"rate": 0}).status_code == 403
    assert httpx.get(f"{server.url}/api/state", headers={"Host": "evil.example"}).status_code == 403
    assert httpx.get(f"{server.url}/api/state").status_code == 200


def test_panel_drives_the_house(panel):
    server, sim = panel
    h = {"X-Sim-Panel": "1"}
    r = httpx.post(f"{server.url}/api/house", json={"preset": "realistic", "backfill_days": 2}, headers=h,
                   timeout=30)  # fmt: skip
    assert (
        r.status_code == 200 and r.json()["summary"]["entities"] > 250 and r.json()["backfill"]["days"] == 2
    )
    r = httpx.post(
        f"{server.url}/api/entities/light.hallway", json={"state": "on", "cause": "user"}, headers=h
    )
    assert r.status_code == 200 and sim.ha.logbook_rows[-1]["context_user_id"] == "sim-owner"
    assert (
        httpx.post(f"{server.url}/api/entities/light.nope", json={"state": "on"}, headers=h).status_code
        == 404
    )
    junk = httpx.get(f"{server.url}/api/entities", params={"junk": "true"}).json()
    assert junk and all(e["junk"] for e in junk)
    r = httpx.put(
        f"{server.url}/api/secrets", json={"anthropic_api_key": "sk-ant-api03-secretsecret"}, headers=h
    )
    assert r.json()["anthropic_api_key"] == "…cret"
    assert "secretsecret" not in httpx.get(f"{server.url}/api/state").text
    r = httpx.post(f"{server.url}/api/faults", json={"target": "learner", "fail_next": 2}, headers=h)
    assert r.status_code == 200 and sim.cfg.faults["learner"].fail_next == 2
    assert httpx.post(f"{server.url}/api/faults", json={"target": "jetson"}, headers=h).status_code == 400
    assert (
        httpx.put(f"{server.url}/api/config", json={"room": {"backend": "x"}}, headers=h).status_code == 400
    )
    state = httpx.get(f"{server.url}/api/state").json()
    assert state["learner"] == {"backend": "sim", "status": None, "model": None}, "no learner in this test"
    assert httpx.post(f"{server.url}/api/learner/train", headers=h).json()["started"] is False
