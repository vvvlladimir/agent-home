"""M3 acceptance, ported to the actors of D40: through the ingress prefix, against the fake Home
Assistant and the fake Claude, the house manager (the orchestrator) acts only through the policy:
shadow vs live, T2 approvals, T3 never, the kill switch from HA and its helper, the policy file, the
inbox and the action log."""

from __future__ import annotations

import io
import json
import zipfile

import httpx
import jsonschema
import pytest
import yaml

from .test_m1_skeleton import OPENAPI, connected, response_schema, status, wait_for

LAMP = "light.living_room_lamp"


def validate(payload, path: str, method: str = "get", code: str = "200") -> None:
    jsonschema.validate(payload, response_schema(path, method, code), cls=jsonschema.Draft202012Validator)


def component(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}", "components": OPENAPI["components"]}


@pytest.fixture
def base(ingress, ha_state):
    connected(ingress.base)
    ha_state.set_state(LAMP, "off")
    assert httpx.put(f"{ingress.base}/api/settings", json={"manager_enabled": True}).status_code == 200
    return ingress.base


def call(domain: str, service: str, entity_id: str, **data) -> dict:
    return {"tool_use": [{"name": "call_service", "input": {"domain": domain, "service": service,
                                                            "entity_id": entity_id, "data": data}}]}  # fmt: skip


def wake(base: str, claude_sim, *script) -> dict:
    """One manual manager run with the model's turns scripted; → the finished run."""
    claude_sim.script[:] = [*script, {"text": "Done."}]
    before = {r["id"] for r in httpx.get(f"{base}/api/runs").json()}
    response = httpx.post(f"{base}/api/manager/wake", json={"reason": "test"})
    assert response.status_code == 202, response.text

    def done():
        runs = [r for r in httpx.get(f"{base}/api/runs").json() if r["id"] not in before]
        return runs and runs[0]["status"] not in ("queued", "running") and runs[0]

    return wait_for(done, 20)


def test_live_needs_both_switches_then_the_light_really_turns_on(base, ha_state, claude_sim):
    httpx.put(f"{base}/api/mode", json={"mode": "live"})
    run = wake(base, claude_sim, call("light", "turn_on", LAMP))
    assert run["shadow"] and ha_state.states[LAMP]["state"] == "off", "orchestrator_mode is still shadow"
    assert httpx.get(f"{base}/api/actions").json()[0]["verdict"] == "shadow"
    httpx.put(f"{base}/api/settings", json={"orchestrator_mode": "live"})
    run = wake(base, claude_sim, call("light", "turn_on", LAMP))
    wait_for(lambda: ha_state.states[LAMP]["state"] == "on")
    assert run["shadow"] is False and run["agent_id"] == "orchestrator" and run["wake_source"] == "manage"
    executed = httpx.get(f"{base}/api/actions", params={"verdict": "executed"}).json()
    assert executed[0]["entity_ids"] == [LAMP] and executed[0]["agent_id"] == "orchestrator"
    assert ha_state.service_calls[-1]["data"]["entity_id"] == LAMP


def test_t2_waits_for_the_owner_and_t3_never_passes(base, ha_state, claude_sim):
    httpx.put(f"{base}/api/mode", json={"mode": "live"})
    httpx.put(f"{base}/api/settings", json={"orchestrator_mode": "live"})
    wake(base, claude_sim, call("climate", "set_temperature", "climate.living_room", temperature=23),
         call("lock", "unlock", "lock.front_door"))  # fmt: skip
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
    assert ha_state.states["lock.front_door"]["state"] == "locked"


def test_kill_switch_from_home_assistant(base, ha_state, claude_sim):
    httpx.put(f"{base}/api/mode", json={"mode": "live"})
    httpx.put(f"{base}/api/settings", json={"orchestrator_mode": "live"})
    ha_state.fire_event("agent_house_kill_switch", {"reason": "panic button"})
    kill = wait_for(lambda: (k := status(base)["kill_switch"])["engaged"] and k)
    assert kill["reason"] == "panic button" and kill["since"]
    refused = httpx.post(f"{base}/api/manager/wake")
    assert refused.status_code == 409 and refused.json()["error"] == "kill_switch"
    assert httpx.post(f"{base}/api/proposals", json={"request": "lamp on"}).status_code == 409
    assert claude_sim.requests == [], "no model is called while the switch is engaged"
    ha_state.fire_event("agent_house_kill_switch", {"engaged": False})
    wait_for(lambda: not status(base)["kill_switch"]["engaged"])
    wake(base, claude_sim, call("light", "turn_on", LAMP))
    wait_for(lambda: ha_state.states[LAMP]["state"] == "on")


def test_kill_switch_helper_entity_mirrors_and_is_off_limits(base, ha_state, claude_sim):
    helper = "input_boolean.agent_house_kill"
    ha_state.set_state(helper, "off")
    bad = httpx.put(f"{base}/api/settings", json={"kill_switch_entity": "light.kitchen_ceiling"})
    assert bad.status_code == 422
    assert httpx.put(f"{base}/api/settings", json={"kill_switch_entity": helper}).status_code == 200
    httpx.put(f"{base}/api/mode", json={"mode": "live"})
    httpx.put(f"{base}/api/settings", json={"orchestrator_mode": "live"})
    wake(base, claude_sim, call("input_boolean", "turn_on", helper))
    denied = httpx.get(f"{base}/api/actions").json()[0]
    assert denied["verdict"] == "denied" and "reserved for the owner" in denied["reason"]
    ha_state.set_state(helper, "on")
    wait_for(lambda: status(base)["kill_switch"]["engaged"])
    ha_state.set_state(helper, "off")
    wait_for(lambda: not status(base)["kill_switch"]["engaged"])


def test_runs_are_listed_and_traced(base, claude_sim):
    run = wake(base, claude_sim, call("light", "turn_on", LAMP))
    full = httpx.get(f"{base}/api/runs/{run['id']}").json()
    validate(full, "/api/runs/{run_id}")
    assert [s["type"] for s in full["trace"]][:2] == ["context", "llm"]
    assert [r["id"] for r in httpx.get(f"{base}/api/runs", params={"agent": "orchestrator"}).json()] == [run["id"]]
    assert httpx.get(f"{base}/api/runs/r_missing").status_code == 404
    assert httpx.get(f"{base}/api/runs", params={"status": "bogus"}).status_code == 422
    for gone in ("replay", "testcase"):  # room-agent runs only (D40)
        assert httpx.post(f"{base}/api/runs/{run['id']}/{gone}").status_code in (404, 405)


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
    """QA-004: t3_whitelist is an add-on option; the API refuses to change it, and lock.front_door
    stays T3 whatever is PUT or imported."""
    policy = httpx.get(f"{base}/api/policy").json()
    assert policy["t3_whitelist"] == []
    lifted = httpx.put(f"{base}/api/policy", json={**policy, "t3_whitelist": ["lock.front_door"],
                                                    "allowed_services": {**policy["allowed_services"],
                                                                         "lock": ["unlock"]}})  # fmt: skip
    assert lifted.status_code == 422 and "add-on option" in lifted.json()["detail"]
    assert httpx.put(f"{base}/api/policy", json=policy).status_code == 200, "GET → PUT round-trips"
    imported = httpx.post(f"{base}/api/config/import", content=yaml.safe_dump(
        {"agent_house_export": 1, "policy": {"t3_whitelist": ["lock.front_door"]}}))  # fmt: skip
    assert imported.status_code == 422
    tiers = {e["entity_id"]: e["tier"] for e in httpx.get(f"{base}/api/entities").json()}
    assert tiers["lock.front_door"] == "T3"


def test_reports_reach_the_inbox_and_one_alarm_stays_open(base, claude_sim):
    report = {"tool_use": [{"name": "report", "input": {"severity": "alarm",
                                                        "summary": "binary_sensor.kitchen_window is open in the rain."}}]}  # fmt: skip
    run = wake(base, claude_sim, report)
    wake(base, claude_sim, report)
    inbox = httpx.get(f"{base}/api/inbox").json()
    validate(inbox, "/api/inbox")
    alarms = [i for i in inbox if i["severity"] == "alarm"]
    assert len(alarms) == 1 and alarms[0]["agent_id"] == "orchestrator" and alarms[0]["status"] == "new"
    assert alarms[0]["run_id"] != run["id"], "the second report updated the open alarm"


def test_diagnostics_include_the_action_log_and_the_policy(base, claude_sim):
    wake(base, claude_sim, call("light", "turn_on", LAMP))
    archive = zipfile.ZipFile(io.BytesIO(httpx.get(f"{base}/api/diagnostics").content))
    assert json.loads(archive.read("actions.json"))[0]["verdict"] == "shadow"
    assert json.loads(archive.read("policy.json"))["t3_whitelist"] == []
    assert json.loads(archive.read("learner.json"))["configured"] is False
