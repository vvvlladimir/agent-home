"""The sim panel's machinery: house generator, life, scenarios, config/secrets, model gateways, control API.

Hermetic: the "real" providers are the fakes on ephemeral ports.
"""

from __future__ import annotations

import json
import stat
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from sim import control, fake_anthropic, fake_ha, fake_jetson, fake_llm, fake_openai, house_gen
from sim.gateways import Gateways, to_anthropic, to_responses
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

    def fire(event_type, data=None):
        if not ha.quiet:
            seen.append({"type": event_type, **(data or {})})
        orig(event_type, data)

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
        cfg.update({"room": {"pace": False, "backend": "mainframe"}})
    assert cfg.room.backend == "fake" and cfg.room.pace is True
    with pytest.raises(ValueError):
        cfg.update({"jetson": {"host": "http://192.168.1.5:8080"}})
    with pytest.raises(ValueError):
        cfg.update({"room": {"nonsense": 1}})
    cfg.update({"room": {"backend": "jetson"}, "jetson": {"host": "192.168.1.5"}})
    assert cfg.metrics_source() == "jetson"
    cfg.set_secret("openai_api_key", "sk-proj-abcdefgh12345678")
    cfg.save()
    assert stat.S_IMODE((tmp_path / "secrets.json").stat().st_mode) == 0o600
    assert "sk-proj" not in (tmp_path / "config.json").read_text()
    assert "sk-proj" not in json.dumps(cfg.public()) and cfg.public()["secrets"]["openai_api_key"] == "…5678"
    again = SimConfig.load(tmp_path)
    assert again.room.backend == "jetson" and again.secret("openai_api_key") == "sk-proj-abcdefgh12345678"
    with pytest.raises(ValueError):
        cfg.set_secret("aws_key", "x")


# ---- gateways ------------------------------------------------------------------------------------
def test_requests_translate_to_claude_and_responses_shapes():
    body = {
        "messages": [
            {"role": "system", "content": "You are the kitchen."},
            {"role": "user", "content": "motion"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "call_service", "arguments": '{"a": 1}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ],
        "tools": [{"type": "function", "function": {"name": "call_service", "description": "d",
                                                    "parameters": {"type": "object", "properties": {}}}}],
        "temperature": 0.7, "top_k": 20, "presence_penalty": 1.5, "max_tokens": 300,
    }  # fmt: skip
    a = to_anthropic(body)
    assert a["system"] == "You are the kitchen." and fake_anthropic.rejected_param(a) is None
    assert a["messages"][1]["content"][0] == {"type": "tool_use", "id": "c1", "name": "call_service",
                                              "input": {"a": 1}}  # fmt: skip
    assert a["messages"][2]["content"][0]["type"] == "tool_result"
    r = to_responses(body)
    assert fake_openai.rejected_param(r) is None and r["instructions"] == "You are the kitchen."
    assert [i.get("type", i.get("role")) for i in r["input"]] == [
        "user",
        "function_call",
        "function_call_output",
    ]


@pytest.fixture
def gw_world(tmp_path):
    """Fakes as both the 'internal' sims and the 'real' upstream providers, gateways in front."""
    llm, jetson = fake_llm.LLMSim(), fake_jetson.JetsonSim()
    real_claude, real_openai = fake_anthropic.AnthropicSim(), fake_openai.OpenAISim()
    real_claude.api_key, real_openai.api_key = "sk-ant-REAL", "sk-proj-REAL"
    servers = {
        "llm": ServerThread(fake_llm.create_app(llm)),
        "anthropic": ServerThread(fake_anthropic.create_app()),
        "openai": ServerThread(fake_openai.create_app()),
        "jetson": ServerThread(fake_jetson.create_app(jetson)),
        "real_claude": ServerThread(fake_anthropic.create_app(real_claude)),
        "real_openai": ServerThread(fake_openai.create_app(real_openai)),
    }
    for s in servers.values():
        s.start()
    cfg = SimConfig.load(tmp_path)
    cfg.room.pace = False
    gw = Gateways(cfg, jetson, {k: servers[k].url for k in ("llm", "anthropic", "openai", "jetson")},
                  upstream={"anthropic": servers["real_claude"].url, "openai": servers["real_openai"].url})  # fmt: skip
    fronts = {
        "llm": ServerThread(gw.llm_app()), "anthropic": ServerThread(gw.cloud_app("anthropic")),
        "openai": ServerThread(gw.cloud_app("openai")), "metrics": ServerThread(gw.metrics_app()),
    }  # fmt: skip
    for s in fronts.values():
        s.start()
    yield {"cfg": cfg, "gw": gw, "fronts": fronts, "llm": llm, "claude": real_claude, "openai": real_openai}
    for s in [*fronts.values(), *servers.values()]:
        s.stop()


CHAT = {"messages": [{"role": "user", "content": "binary_sensor.kitchen_motion is on"}],
        "tools": [{"type": "function", "function": {"name": "report", "parameters": {"type": "object"}}}],
        "max_tokens": 200}  # fmt: skip


def test_fake_backend_and_injected_faults(gw_world):
    url = gw_world["fronts"]["llm"].url
    r = httpx.post(f"{url}/v1/chat/completions", json=CHAT)
    assert (
        r.status_code == 200
        and r.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "report"
    )
    gw_world["cfg"].faults["llm"].fail_next = 1
    assert httpx.post(f"{url}/v1/chat/completions", json=CHAT).status_code == 500
    assert httpx.post(f"{url}/v1/chat/completions", json=CHAT).status_code == 200
    assert [row["status"] for row in gw_world["gw"].log.list()][:2] == [200, 500]


def test_room_agents_on_claude_answer_in_llama_server_shape(gw_world):
    cfg, url = gw_world["cfg"], gw_world["fronts"]["llm"].url
    cfg.update({"room": {"backend": "anthropic"}})
    assert httpx.post(f"{url}/v1/chat/completions", json=CHAT).status_code == 401  # no key saved yet
    cfg.set_secret("anthropic_api_key", "sk-ant-REAL")
    gw_world["claude"].script.append({"tool_use": [{"name": "report", "input": {"summary": "hi"}}]})
    with httpx.stream("POST", f"{url}/v1/chat/completions", json={**CHAT, "stream": True}) as r:
        lines = [ln for ln in r.iter_lines() if ln.startswith("data: ") and ln != "data: [DONE]"]
    chunks = [json.loads(ln[6:]) for ln in lines]
    args = "".join(c["choices"][0]["delta"].get("tool_calls", [{}])[0].get("function", {}).get("arguments", "")
                   for c in chunks if c["choices"])  # fmt: skip
    assert json.loads(args) == {"summary": "hi"}
    assert chunks[-1]["timings"]["prompt_n"] > 0 and chunks[-1]["model"] == "qwen35-4b-balanced"
    sent = gw_world["claude"].requests[-1]["body"]
    assert sent["model"] == "claude-haiku-5-5" and "temperature" not in sent
    assert httpx.get(f"{url}/health").json() == {"status": "ok"}
    assert httpx.get(f"{url}/props").json()["default_generation_settings"]["n_ctx"] == 8192


def test_room_agents_on_openai_and_the_jetson_context_limit(gw_world):
    cfg, url = gw_world["cfg"], gw_world["fronts"]["llm"].url
    cfg.update(
        {"room": {"backend": "openai", "pace": True, "prefill_tps": 1e6, "decode_tps": 1e6, "n_ctx": 512}}
    )
    cfg.set_secret("openai_api_key", "sk-proj-REAL")
    big = {"messages": [{"role": "user", "content": "x" * 4000}]}
    r = httpx.post(f"{url}/v1/chat/completions", json=big)
    assert r.status_code == 400 and r.json()["error"]["type"] == "exceed_context_size_error"
    gw_world["openai"].script.append({"function_calls": [{"name": "report", "arguments": {"summary": "ok"}}]})
    r = httpx.post(f"{url}/v1/chat/completions", json=CHAT)
    msg = r.json()["choices"][0]
    assert (
        msg["finish_reason"] == "tool_calls"
        and msg["message"]["tool_calls"][0]["function"]["name"] == "report"
    )
    assert gw_world["openai"].requests[-1]["body"]["model"] == "gpt-6-luna"


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


def test_metrics_gateway_serves_the_fake_jetson(gw_world):
    url = gw_world["fronts"]["metrics"].url
    assert httpx.get(f"{url}/metrics.json").json()["source"] == "sim"
    r = httpx.post(f"{url}/profile/qwen35-2b-fast", headers={"Authorization": "Bearer jetson-sim"})
    assert r.json()["active_profile"] == "qwen35-2b-fast"


# ---- control API ----------------------------------------------------------------------------------
@pytest.fixture
def panel(tmp_path):
    ha, llm, jetson = fake_ha.HAState.default(), fake_llm.LLMSim(), fake_jetson.JetsonSim()
    cfg = SimConfig.load(tmp_path)
    sim = control.Sim(ha, llm, jetson, cfg, Gateways(cfg, jetson, {}))
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
    r = httpx.post(f"{server.url}/api/faults", json={"target": "jetson", "temp_tj_c": 500}, headers=h)
    assert r.status_code == 200 and sim.jetson.temp_tj_c == 110
    assert (
        httpx.put(f"{server.url}/api/config", json={"room": {"backend": "x"}}, headers=h).status_code == 400
    )
