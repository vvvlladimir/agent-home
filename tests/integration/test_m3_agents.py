"""M3 acceptance: room agents in Shadow Mode, through the ingress prefix, against the fake Home
Assistant and the fake llama-server: CRUD, event wakes, shadow vs live, T2 approvals, the kill
switch from HA, traces with replay and copy-as-test-case."""

from __future__ import annotations

import httpx
import jsonschema
import pytest
import yaml

from .test_m1_skeleton import OPENAPI, connected, response_schema, status, wait_for

MOTION = "binary_sensor.living_room_motion"
LAMP = "light.living_room_lamp"
AGENT = {
    "id": "living_room",
    "name": "Living room",
    "room": "living_room",
    "wake_rules": {
        "events": [
            {"entities": [MOTION], "to": ["on"], "debounce_s": 0, "cooldown_s": 0, "reason": "Motion."}
        ]
    },
}


def validate(payload, path: str, method: str = "get", code: str = "200") -> None:
    jsonschema.validate(payload, response_schema(path, method, code), cls=jsonschema.Draft202012Validator)


def component(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}", "components": OPENAPI["components"]}


@pytest.fixture
def base(ingress, ha_state):
    connected(ingress.base)
    wait_for(lambda: status(ingress.base)["llm"]["state"] == "ok")
    ha_state.set_state(LAMP, "off")
    return ingress.base


def create(base: str, **changes) -> dict:
    response = httpx.post(f"{base}/api/agents", json={**AGENT, **changes})
    assert response.status_code == 201, response.text
    return response.json()


def motion(ha_state) -> None:
    ha_state.set_state(MOTION, "off")
    ha_state.set_state(MOTION, "on")


def finished_runs(base: str, count: int = 1, **params) -> list[dict]:
    def done():
        runs = httpx.get(f"{base}/api/runs", params=params).json()
        return len(runs) >= count and all(r["status"] not in ("queued", "running") for r in runs) and runs

    return wait_for(done)


def test_agent_crud_follows_the_contract(base):
    assert httpx.get(f"{base}/api/agents").json() == []
    created = create(base)
    assert created["mode"] == "shadow" and created["limits"]["max_steps"] == 5, "defaults are filled in"
    listed = httpx.get(f"{base}/api/agents").json()
    validate(listed, "/api/agents")
    assert listed[0]["effective_mode"] == "shadow" and listed[0]["entity_count"] == 10
    assert "10 in total" in listed[0]["scope"], "QA-009: the count is explained in words"
    assert httpx.get(f"{base}/api/agents/living_room").json() == created
    areas = {a["area_id"]: a["agent_id"] for a in httpx.get(f"{base}/api/areas").json()}
    assert areas["living_room"] == "living_room" and areas["kitchen"] is None
    assert status(base)["counts"]["agents"] == 1

    updated = httpx.put(f"{base}/api/agents/living_room", json={**created, "name": "Lounge"})
    assert updated.status_code == 200 and updated.json()["name"] == "Lounge"
    duplicate = httpx.post(f"{base}/api/agents", json=AGENT)
    assert duplicate.status_code == 409 and duplicate.json()["error"] == "conflict"
    for response in (
        httpx.post(f"{base}/api/agents", json={**AGENT, "id": "x2", "mode": "yolo"}),
        httpx.post(f"{base}/api/agents", content=b"not json"),
        httpx.put(f"{base}/api/agents/living_room", json={**AGENT, "id": "other"}),
    ):
        assert response.status_code == 422 and response.json()["error"] == "invalid", response.text
    # QA-005/006: a typo in the room or an entity is refused at save time, and says what is wrong
    typo_room = httpx.post(f"{base}/api/agents", json={**AGENT, "id": "office", "room": "livng-room"})
    assert typo_room.status_code == 422 and "Did you mean 'living_room'?" in typo_room.json()["detail"]
    typo_entity = httpx.put(
        f"{base}/api/agents/living_room",
        json={**created, "entities": ["switch.fan_typo"], "entities_mode": "pinned"},
    )
    assert typo_entity.status_code == 422 and "switch.fan_typo" in typo_entity.json()["detail"]
    assert httpx.put(f"{base}/api/agents/ghost", json=AGENT).status_code == 404
    assert httpx.get(f"{base}/api/agents/ghost").status_code == 404
    assert httpx.delete(f"{base}/api/agents/living_room").status_code == 204
    assert httpx.delete(f"{base}/api/agents/living_room").status_code == 404
    assert httpx.get(f"{base}/api/agents").json() == []


def test_agents_survive_a_restart_as_yaml(base, settings):
    create(base)
    stored = yaml.safe_load((settings.data_dir / "agents" / "living_room.yaml").read_text())
    assert stored["id"] == "living_room" and stored["mode"] == "shadow"


def test_prompt_preview_for_an_unsaved_config(base, llm_sim):
    before = len(llm_sim.requests)
    preview = httpx.post(
        f"{base}/api/agents/new/prompt-preview", json={**AGENT, "id": "new", "max_entities": 3}
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    validate(body, "/api/agents/{agent_id}/prompt-preview", "post")
    assert body["entities_included"] == 3 and len(body["entities_dropped"]) == 7
    assert "Living Room" in body["system"] and f"{LAMP}: off" in body["user"]
    assert 0 < body["tokens_estimate"] <= body["budget"]
    assert len(llm_sim.requests) == before, "a preview never calls the model"
    assert httpx.post(f"{base}/api/agents/ghost/prompt-preview").status_code == 404


def test_motion_wakes_the_agent_and_shadow_mode_only_records(base, ha_state, llm_sim):
    create(base)
    calls_before = len(ha_state.service_calls)
    motion(ha_state)
    run = finished_runs(base)[0]
    validate([run], "/api/runs")
    assert run["status"] == "ok" and run["shadow"] is True and run["wake_source"] == "event"
    assert "changed off → on. Motion." in run["wake_reason"]
    full = httpx.get(f"{base}/api/runs/{run['id']}").json()
    validate(full, "/api/runs/{run_id}")
    assert [s["type"] for s in full["trace"]] == ["context", "llm", "tool", "policy", "llm", "final"]
    actions = httpx.get(f"{base}/api/actions").json()
    validate(actions, "/api/actions")
    assert [(a["entity_ids"], a["verdict"], a["run_id"]) for a in actions] == [([LAMP], "shadow", run["id"])]
    assert len(ha_state.service_calls) == calls_before and ha_state.states[LAMP]["state"] == "off"
    kinds = {e["kind"] for e in httpx.get(f"{base}/api/events").json()}
    assert {"wake", "run", "action"} <= kinds
    assert status(base)["counts"]["runs_today"] == 1
    assert httpx.get(f"{base}/api/agents").json()[0]["wakes_last_hour"] == 1
    assert "Wake reason:" in llm_sim.requests[-1]["messages"][1]["content"]


def test_live_needs_both_switches_then_the_light_really_turns_on(base, ha_state):
    create(base)
    httpx.put(f"{base}/api/mode", json={"mode": "live"})
    motion(ha_state)
    finished_runs(base)
    assert ha_state.states[LAMP]["state"] == "off", "the agent's own mode is still shadow"
    httpx.put(f"{base}/api/agents/living_room", json={**AGENT, "mode": "live"})
    assert httpx.get(f"{base}/api/agents").json()[0]["effective_mode"] == "live"
    motion(ha_state)
    wait_for(lambda: ha_state.states[LAMP]["state"] == "on")
    runs = finished_runs(base, 2)
    assert runs[0]["shadow"] is False
    assert httpx.get(f"{base}/api/actions", params={"verdict": "executed"}).json()[0]["entity_ids"] == [LAMP]
    assert ha_state.service_calls[-1]["data"]["entity_id"] == LAMP


def scripted_call(domain: str, service: str, entity_id: str, **data) -> dict:
    arguments = {"domain": domain, "service": service, "entity_id": entity_id, "data": data}
    return {"tool_calls": [{"name": "call_service", "arguments": arguments}]}


def test_t2_waits_for_the_owner_and_t3_never_passes(base, ha_state, llm_sim):
    create(base, mode="live")
    httpx.put(f"{base}/api/mode", json={"mode": "live"})
    llm_sim.script = [
        scripted_call("climate", "set_temperature", "climate.living_room", temperature=23),
        scripted_call("lock", "unlock", "lock.front_door"),
        {"text": "Asked for approval."},
    ]
    run_id = httpx.post(f"{base}/api/agents/living_room/wake", json={"reason": "It is cold."}).json()[
        "run_id"
    ]
    wait_for(lambda: httpx.get(f"{base}/api/runs/{run_id}").json()["status"] == "ok")
    pending = httpx.get(f"{base}/api/actions", params={"verdict": "pending"}).json()
    assert len(pending) == 1 and pending[0]["tier"] == "T2" and pending[0]["expires"]
    assert status(base)["counts"]["pending_approvals"] == 1
    assert httpx.get(f"{base}/api/actions", params={"verdict": "denied"}).json()[0]["entity_ids"] == [
        "lock.front_door"
    ]
    assert ha_state.states["climate.living_room"]["attributes"]["temperature"] == 21
    assert ha_state.states["lock.front_door"]["state"] == "locked"

    approved = httpx.post(f"{base}/api/approvals/{pending[0]['id']}", json={"approve": True})
    assert approved.status_code == 200, approved.text
    jsonschema.validate(approved.json(), component("Action"), cls=jsonschema.Draft202012Validator)
    assert approved.json()["verdict"] == "approved" and approved.json()["decided_by"] == "owner"
    wait_for(lambda: ha_state.states["climate.living_room"]["attributes"]["temperature"] == 23)
    again = httpx.post(f"{base}/api/approvals/{pending[0]['id']}", json={"approve": True})
    assert again.status_code == 409 and again.json()["error"] == "conflict"
    assert status(base)["counts"]["pending_approvals"] == 0
    assert httpx.get(f"{base}/api/actions", params={"verdict": "nonsense"}).status_code == 422


def test_kill_switch_from_home_assistant(base, ha_state, llm_sim):
    create(base, mode="live")
    httpx.put(f"{base}/api/mode", json={"mode": "live"})
    ha_state.fire_event("agent_house_kill_switch", {"reason": "panic button"})
    kill = wait_for(lambda: (k := status(base)["kill_switch"])["engaged"] and k)
    assert kill["reason"] == "panic button" and kill["since"]
    refused = httpx.post(f"{base}/api/agents/living_room/wake")
    assert refused.status_code == 409 and refused.json()["error"] == "kill_switch"
    requests_before = len(llm_sim.requests)
    motion(ha_state)
    wait_for(
        lambda: any(
            "kill switch" in e["summary"]
            for e in httpx.get(f"{base}/api/events", params={"kind": "gate_drop"}).json()
        )
    )
    assert len(llm_sim.requests) == requests_before and ha_state.states[LAMP]["state"] == "off"
    ha_state.fire_event("agent_house_kill_switch", {"engaged": False})
    wait_for(lambda: not status(base)["kill_switch"]["engaged"])
    motion(ha_state)
    wait_for(lambda: ha_state.states[LAMP]["state"] == "on")


def test_kill_switch_helper_entity_mirrors_and_is_off_limits(base, ha_state, llm_sim):
    helper = "input_boolean.agent_house_kill"
    ha_state.set_state(helper, "off")
    bad = httpx.put(f"{base}/api/settings", json={"kill_switch_entity": "light.kitchen_ceiling"})
    assert bad.status_code == 422
    assert httpx.put(f"{base}/api/settings", json={"kill_switch_entity": helper}).status_code == 200
    create(base, mode="live", entities=[MOTION, LAMP, helper])
    httpx.put(f"{base}/api/mode", json={"mode": "live"})
    llm_sim.script = [scripted_call("input_boolean", "turn_on", helper), {"text": "x"}]
    run_id = httpx.post(f"{base}/api/agents/living_room/wake").json()["run_id"]
    wait_for(lambda: httpx.get(f"{base}/api/runs/{run_id}").json()["status"] == "ok")
    denied = httpx.get(f"{base}/api/actions").json()[0]
    assert denied["verdict"] == "denied" and "reserved for the owner" in denied["reason"]
    ha_state.set_state(helper, "on")
    wait_for(lambda: status(base)["kill_switch"]["engaged"])
    ha_state.set_state(helper, "off")
    wait_for(lambda: not status(base)["kill_switch"]["engaged"])


def test_replay_and_copy_as_test_case(base, ha_state):
    create(base, mode="live")
    httpx.put(f"{base}/api/mode", json={"mode": "live"})
    motion(ha_state)
    original = finished_runs(base)[0]
    wait_for(lambda: ha_state.states[LAMP]["state"] == "on")
    ha_state.set_state(LAMP, "off")
    calls = len(ha_state.service_calls)

    replay = httpx.post(f"{base}/api/runs/{original['id']}/replay")
    assert replay.status_code == 202
    replayed = wait_for(
        lambda: (r := httpx.get(f"{base}/api/runs/{replay.json()['run_id']}").json())["status"] == "ok" and r
    )
    assert replayed["replay_of"] == original["id"] and replayed["shadow"] is True
    assert len(ha_state.service_calls) == calls and ha_state.states[LAMP]["state"] == "off"
    assert [r["id"] for r in httpx.get(f"{base}/api/runs", params={"before": replayed["id"]}).json()] == [
        original["id"]
    ]

    testcase = httpx.get(f"{base}/api/runs/{original['id']}/testcase")
    assert testcase.headers["content-type"].startswith("text/yaml")
    doc = yaml.safe_load(testcase.text)
    assert doc["scenarios"][0]["expect"]["calls"] == [
        {"tool": "call_service", "args": {"service": "turn_on", "entity_id": LAMP}}
    ]
    assert httpx.post(f"{base}/api/runs/r_missing/replay").status_code == 404
    assert httpx.get(f"{base}/api/runs/r_missing/testcase").status_code == 404
    assert httpx.get(f"{base}/api/runs/r_missing").status_code == 404
    assert httpx.get(f"{base}/api/runs", params={"status": "bogus"}).status_code == 422


def test_policy_can_be_read_and_tightened(base, settings):
    policy = httpx.get(f"{base}/api/policy").json()
    assert policy["domain_tiers"]["light"] == "T0" and policy["approval_ttl_s"] == 900
    tightened = httpx.put(
        f"{base}/api/policy", json={"domain_tiers": {**policy["domain_tiers"], "light": "T2"}}
    )
    assert tightened.status_code == 200 and tightened.json()["domain_tiers"]["light"] == "T2"
    tiers = {e["entity_id"]: e["tier"] for e in httpx.get(f"{base}/api/entities").json()}
    assert tiers[LAMP] == "T2" and tiers["lock.front_door"] == "T3"
    assert yaml.safe_load((settings.data_dir / "policy.yaml").read_text())["domain_tiers"]["light"] == "T2"
    for bad in ({"domain_tiers": {"light": "T9"}}, {"unknown_key": 1}, ["list"]):
        assert httpx.put(f"{base}/api/policy", json=bad).status_code == 422


def test_nothing_reachable_from_the_api_lifts_a_t3(base):
    """QA-004: t3_whitelist is an add-on option; the API refuses to change it, a looser agent
    override is refused, and lock.front_door stays T3 whatever is PUT."""
    policy = httpx.get(f"{base}/api/policy").json()
    assert policy["t3_whitelist"] == []
    lifted = httpx.put(f"{base}/api/policy", json={**policy, "t3_whitelist": ["lock.front_door"],
                                                    "allowed_services": {**policy["allowed_services"],
                                                                         "lock": ["unlock"]}})  # fmt: skip
    assert lifted.status_code == 422 and "add-on option" in lifted.json()["detail"]
    assert httpx.put(f"{base}/api/policy", json=policy).status_code == 200, "GET → PUT round-trips"
    override = httpx.post(f"{base}/api/agents", json={**AGENT, "tier_overrides": {"lock.front_door": "T0"}})
    assert override.status_code == 422 and "only make an entity stricter" in override.json()["detail"]
    imported = httpx.post(f"{base}/api/config/import", json={"policy": {"t3_whitelist": ["lock.front_door"]}})
    assert imported.status_code == 422
    tiers = {e["entity_id"]: e["tier"] for e in httpx.get(f"{base}/api/entities").json()}
    assert tiers["lock.front_door"] == "T3"


def test_reports_reach_the_inbox(base, llm_sim):
    create(base)
    llm_sim.script = [
        {
            "tool_calls": [
                {
                    "name": "report",
                    "arguments": {"severity": "escalate", "summary": "Window open, heating on."},
                }
            ]
        },
        {"text": "Reported."},
    ]
    run_id = httpx.post(f"{base}/api/agents/living_room/wake").json()["run_id"]
    wait_for(lambda: httpx.get(f"{base}/api/runs/{run_id}").json()["status"] == "escalated")
    inbox = httpx.get(f"{base}/api/inbox").json()
    validate(inbox, "/api/inbox")
    assert inbox[0]["severity"] == "alarm" and inbox[0]["run_id"] == run_id and inbox[0]["status"] == "new"


def test_diagnostics_include_agents_and_the_action_log(base, ha_state):
    import io
    import json
    import zipfile

    create(base)
    motion(ha_state)
    finished_runs(base)
    archive = zipfile.ZipFile(io.BytesIO(httpx.get(f"{base}/api/diagnostics").content))
    assert json.loads(archive.read("agents.json"))[0]["id"] == "living_room"
    assert json.loads(archive.read("actions.json"))[0]["verdict"] == "shadow"
    assert json.loads(archive.read("policy.json"))["t3_whitelist"] == []
