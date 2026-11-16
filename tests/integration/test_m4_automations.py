"""M4 acceptance, through the ingress prefix against fake Home Assistant and fake Claude / OpenAI:
drafts never reach HA, approve deploys `agent_house_*` with versions and rollback, the orchestrator
drafts in the cloud (redacted, within budget; D40: no local model), the kill switch halts it all, the
owner's own automations are never touched."""

from __future__ import annotations

import copy

import httpx
import jsonschema
import pytest

from app.config import Settings
from app.main import create_app
from sim import fake_anthropic, fake_ingress
from sim.server import ServerThread

from .test_m1_skeleton import OPENAPI, connected, status, wait_for

GOOD = """
alias: Lamp on motion
triggers:
  - trigger: state
    entity_id: binary_sensor.living_room_motion
    to: "on"
actions:
  - action: light.turn_on
    target: {entity_id: light.living_room_lamp}
"""
INGRESS_PREFIX = "/api/hassio_ingress/simtoken"


def validate(payload, path: str, method: str = "get", code: str = "200") -> None:
    schema = OPENAPI["paths"][path][method]["responses"][code]["content"]["application/json"]["schema"]
    jsonschema.validate(
        payload, {**schema, "components": OPENAPI["components"]}, cls=jsonschema.Draft202012Validator
    )


@pytest.fixture
def cloud():
    sim = fake_anthropic.AnthropicSim()
    with ServerThread(fake_anthropic.create_app(sim)) as server:
        server.sim = sim
        yield server


def serve(settings: Settings, **changes):
    """The app with changed settings, behind the fake ingress."""
    app = create_app(settings.model_copy(update=changes))
    app_server = ServerThread(app)
    app_server.__enter__()
    ingress = ServerThread(fake_ingress.create_app(app_server.url))
    ingress.__enter__()
    ingress.base = ingress.url + INGRESS_PREFIX
    ingress.app = app
    ingress.close = lambda: (ingress.__exit__(None, None, None), app_server.__exit__(None, None, None))
    connected(ingress.base)
    return ingress


@pytest.fixture
def base(ingress):
    connected(ingress.base)
    return ingress.base


def test_lifecycle_end_to_end_against_fake_ha(base, ha_state):
    owners = copy.deepcopy(ha_state.automations)
    created = httpx.post(f"{base}/api/proposals", json={"yaml": GOOD, "rationale": "evenings"})
    assert created.status_code == 201, created.text
    proposal = created.json()
    validate(proposal, "/api/proposals", "post", "201")
    assert proposal["status"] == "draft", proposal["checks"]
    assert {c["name"]: c["ok"] for c in proposal["checks"]} == {
        "yaml": True,
        "schema": True,
        "policy": True,
        "ha_validate": True,
    }  # the real client sends plural keys and the fake answers like real HA
    pid, ha_id = proposal["id"], proposal["ha_id"]
    assert ha_id == "agent_house_lamp_on_motion"

    assert (
        httpx.put(f"{base}/api/proposals/{pid}", json={"yaml": GOOD.replace("Lamp", "Lamp 2")}).json()[
            "status"
        ]
        == "draft"
    )
    evidence = httpx.post(f"{base}/api/proposals/{pid}/evidence", json={"days": 2})
    assert evidence.status_code == 200, evidence.text
    validate(evidence.json(), "/api/proposals/{proposal_id}/evidence", "post")
    assert ha_state.config_writes == [], "drafts, edits and evidence never write to HA"
    assert status(base)["counts"]["draft_proposals"] == 1

    approved = httpx.post(f"{base}/api/proposals/{pid}/approve")
    assert approved.status_code == 200 and approved.json()["status"] == "deployed"
    assert [w["id"] for w in ha_state.config_writes] == [ha_id]
    assert ha_state.automations[ha_id]["alias"] == "Lamp 2 on motion"
    deployed = wait_for(
        lambda: (d := httpx.get(f"{base}/api/automations").json()) and d[0]["present_in_ha"] and d
    )
    validate(deployed, "/api/automations")
    assert deployed[0]["enabled"] and deployed[0]["current_version"] == 1

    change = httpx.post(
        f"{base}/api/proposals",
        json={"yaml": GOOD.replace("living_room_lamp", "kitchen_counter"), "ha_id": ha_id},
    ).json()
    assert change["status"] == "draft" and "+    entity_id: light.kitchen_counter" in change["diff"]
    assert httpx.post(f"{base}/api/proposals/{change['id']}/approve").status_code == 200
    rolled = httpx.post(f"{base}/api/automations/{ha_id}/rollback", json={"version": 1})
    assert rolled.status_code == 200 and rolled.json()["current_version"] == 3
    validate(rolled.json(), "/api/automations/{ha_id}/rollback", "post")
    assert ha_state.automations[ha_id]["actions"][0]["target"]["entity_id"] == "light.living_room_lamp"

    off = httpx.put(f"{base}/api/automations/{ha_id}/enabled", json={"enabled": False})
    assert off.status_code == 200
    entity = off.json()["entity_id"]
    wait_for(lambda: ha_state.states[entity]["state"] == "off")
    assert httpx.delete(f"{base}/api/automations/{ha_id}").status_code == 204
    assert ha_id not in ha_state.automations and httpx.get(f"{base}/api/automations").json() == []

    for owner_id in owners:
        for call in (
            httpx.put(f"{base}/api/automations/{owner_id}/enabled", json={"enabled": False}),
            httpx.delete(f"{base}/api/automations/{owner_id}"),
            httpx.post(f"{base}/api/proposals", json={"yaml": GOOD, "ha_id": owner_id}),
        ):
            assert call.status_code == 404, call.text
    assert all(ha_state.automations[k] == v for k, v in owners.items()), (
        "the owner's automations are untouched"
    )
    assert all(w["id"].startswith("agent_house_") for w in ha_state.config_writes)


def test_blocked_drafts_and_the_kill_switch(base, ha_state):
    t3 = GOOD.replace("light.turn_on", "lock.lock").replace("light.living_room_lamp", "lock.front_door")
    blocked = httpx.post(f"{base}/api/proposals", json={"yaml": t3}).json()
    assert blocked["status"] == "blocked"
    assert httpx.post(f"{base}/api/proposals/{blocked['id']}/approve").status_code == 409
    good = httpx.post(f"{base}/api/proposals", json={"yaml": GOOD}).json()
    httpx.put(f"{base}/api/kill-switch", json={"engaged": True, "reason": "test"})
    refused = httpx.post(f"{base}/api/proposals/{good['id']}/approve")
    assert refused.status_code == 409 and refused.json()["error"] == "kill_switch"
    drafted = httpx.post(f"{base}/api/proposals", json={"request": "lamp on at motion"})
    assert drafted.status_code == 409 and drafted.json()["error"] == "kill_switch"
    assert ha_state.config_writes == []
    httpx.put(f"{base}/api/kill-switch", json={"engaged": False})
    rejected = httpx.post(f"{base}/api/proposals/{good['id']}/reject", json={"note": "not now"}).json()
    assert rejected["status"] == "rejected" and rejected["note"] == "not now"
    assert httpx.post(f"{base}/api/proposals", json={}).status_code == 400
    listed = httpx.get(f"{base}/api/proposals", params={"status": "blocked"}).json()
    validate(listed, "/api/proposals")
    assert [p["id"] for p in listed] == [blocked["id"]]


def submit_step(yaml: str, title: str = "Lamp on motion") -> dict:
    return {"tool_use": [{"name": "submit_automation", "input": {
        "title": title, "rationale": "dark evenings", "expected_triggers": "motion", "yaml": yaml}}]}  # fmt: skip


def test_the_orchestrator_drafts_and_fixes_its_draft(base, claude_sim, ha_state):
    bad = GOOD.replace("light.living_room_lamp", "lock.front_door")
    claude_sim.script[:] = [submit_step(bad), submit_step(GOOD), {"text": "Submitted."}]
    response = httpx.post(f"{base}/api/proposals", json={"request": "Turn the lamp on at motion", "room": "living_room"},
                          timeout=30)  # fmt: skip
    assert response.status_code == 201, response.text
    proposal = response.json()
    assert proposal["status"] == "draft" and proposal["title"] == "Lamp on motion"
    first_prompt = claude_sim.requests[0]["body"]["messages"][0]["content"]
    assert "light.living_room_lamp" in first_prompt and "light.kitchen" not in first_prompt
    assert "lock.front_door" not in first_prompt, "only the room's entities"
    run = httpx.get(f"{base}/api/runs/{proposal['run_id']}").json()
    assert run["agent_id"] == "orchestrator" and run["provider"] == "anthropic" and run["status"] == "ok"
    assert [s["data"]["status"] for s in run["trace"] if s["type"] == "tool"] == ["blocked", "draft"]
    assert ha_state.config_writes == []


def test_cloud_drafts_are_redacted_and_paid_from_the_budget(settings, cloud, ha_state):
    ha_state.set_state(
        "person.owner", "home", {"friendly_name": "Anna Schmidt", "latitude": 52.52, "longitude": 13.405}
    )
    app = serve(settings, orchestrator_provider="anthropic", anthropic_api_key=cloud.sim.api_key,
                anthropic_base_url=cloud.url, daily_budget_eur=1.0)  # fmt: skip
    try:
        cloud.sim.script[:] = [
            {"tool_use": [{"name": "submit_automation", "input": {
                "title": "Welcome", "rationale": "r", "expected_triggers": "arrival",
                "yaml": GOOD.replace("binary_sensor.living_room_motion", "person.person_1").replace('"on"', "home"),
            }}]},
            {"text": "Done."},
        ]  # fmt: skip
        response = httpx.post(
            f"{app.base}/api/proposals",
            json={
                "request": "When person.owner (Anna Schmidt, anna@example.com) gets home to Sim Home, lamp on"
            },
            timeout=30,
        )
        assert response.status_code == 201, response.text
        proposal = response.json()
        assert "entity_id: person.owner" in proposal["yaml"], "aliases are restored in the answer"
        sent = str([r["body"] for r in cloud.sim.requests])
        for secret in ("person.owner", "Anna", "Schmidt", "anna@example.com", "Sim Home", "52.52"):
            assert secret not in sent, secret
        first = cloud.sim.requests[0]["body"]
        assert first["model"] == "claude-haiku-5-5" and first["tools"][0]["strict"] is True
        assert "temperature" not in first and "tool_choice" not in first
        budget = status(app.base)["orchestrator"]["budget"]
        assert 0 < budget["spent_today_eur"] < 0.01 and budget["tokens_in_today"] > 0

        # the hard stop: a limit below the worst case of one call refuses before any request
        cloud.sim.requests.clear()
        app.app.state.ctx.settings.daily_budget_eur = budget["spent_today_eur"] + 0.0001
        refused = httpx.post(f"{app.base}/api/proposals", json={"request": "lamp on at motion"}, timeout=30)
        assert refused.status_code == 503 and refused.json()["error"] == "budget"
        assert cloud.sim.requests == []
        assert status(app.base)["orchestrator"]["effective"] in ("anthropic", "none")
    finally:
        app.close()


def test_status_and_contract(base):
    orchestrator = status(base)["orchestrator"]
    assert orchestrator["provider"] == "auto" and orchestrator["effective"] == "anthropic"
    assert orchestrator["choices"] == {"anthropic": True, "openai": False}
    assert orchestrator["budget"]["daily_limit_eur"] == 1.0 and orchestrator["budget"]["exhausted"] is False
    paths = [
        p
        for p, ops in OPENAPI["paths"].items()
        for op in ops.values()
        if isinstance(op, dict) and op.get("x-milestone") == "M4"
    ]
    assert "/api/proposals" in paths and "/api/automations/{ha_id}/rollback" in paths


@pytest.fixture
def openai_cloud():
    from sim import fake_openai

    sim = fake_openai.OpenAISim()
    with ServerThread(fake_openai.create_app(sim)) as server:
        server.sim = sim
        yield server


def test_openai_drafts_through_the_responses_api_redacted_and_budgeted(settings, openai_cloud, ha_state):
    ha_state.set_state("person.owner", "home", {"friendly_name": "Anna Schmidt"})
    sim = openai_cloud.sim
    app = serve(settings, orchestrator_provider="openai", openai_api_key=sim.api_key,
                openai_base_url=openai_cloud.url + "/v1")  # fmt: skip
    try:
        bad = GOOD.replace("light.living_room_lamp", "lock.front_door")
        submit = {"name": "submit_automation", "arguments": {
            "title": "Welcome", "rationale": "r", "expected_triggers": "arrival", "yaml": bad}}  # fmt: skip
        fixed = {**submit, "arguments": {**submit["arguments"], "yaml": GOOD.replace(
            "binary_sensor.living_room_motion", "person.person_1").replace('"on"', "home")}}  # fmt: skip
        sim.script[:] = [{"function_calls": [submit]}, {"function_calls": [fixed]}, {"text": "Done."}]
        response = httpx.post(f"{app.base}/api/proposals",
                              json={"request": "When person.owner (Anna Schmidt) gets home, lamp on"}, timeout=30)  # fmt: skip
        assert response.status_code == 201, response.text
        proposal = response.json()
        assert proposal["status"] == "draft" and "entity_id: person.owner" in proposal["yaml"]
        bodies = [r["body"] for r in sim.requests]
        assert "Anna" not in str(bodies) and "person.owner" not in str(bodies)
        first, second = bodies[0], bodies[1]
        assert first["model"] == "gpt-6.1-sol" and first["store"] is False
        assert (
            first["tools"][0]["strict"] is True
            and first["tools"][0]["parameters"]["additionalProperties"] is False
        )
        kinds = [i.get("type") or i.get("role") for i in second["input"]]
        assert kinds == ["user", "reasoning", "function_call", "function_call_output"], (
            "reasoning passed back"
        )
        assert second["input"][1]["encrypted_content"] == "gAAAA-sim"
        orchestrator = status(app.base)["orchestrator"]
        assert orchestrator["effective"] == "openai" and orchestrator["model"] == "gpt-6.1-sol"
        assert orchestrator["budget"]["spent_today_eur"] > 0
        run = httpx.get(f"{app.base}/api/runs/{proposal['run_id']}").json()
        assert run["provider"] == "openai"
    finally:
        app.close()


def test_auto_falls_back_from_claude_to_openai(settings, openai_cloud, claude_sim):
    app = serve(settings, orchestrator_provider="auto", openai_api_key=openai_cloud.sim.api_key,
                openai_base_url=openai_cloud.url + "/v1")  # fmt: skip
    try:
        claude_sim.script[:] = [{"status": 529}] * 6
        decline = {"function_calls": [{"name": "decline", "arguments": {"reason": "no attic"}}]}
        openai_cloud.sim.script[:] = [decline, decline]
        response = httpx.post(
            f"{app.base}/api/proposals", json={"request": "lamp on in the attic"}, timeout=60
        )
        assert response.status_code == 422 and "no attic" in response.json()["detail"]
        assert len(claude_sim.requests) == 2 and len(openai_cloud.sim.requests) == 2
    finally:
        app.close()


def test_auto_skips_clouds_without_a_key_and_local_reads_as_auto(settings, openai_cloud, claude_sim):
    app = serve(settings, orchestrator_provider="local", anthropic_api_key="",
                openai_api_key=openai_cloud.sim.api_key, openai_base_url=openai_cloud.url + "/v1")  # fmt: skip
    try:
        orchestrator = status(app.base)["orchestrator"]
        assert orchestrator["provider"] == "auto" and orchestrator["effective"] == "openai"
        openai_cloud.sim.script[:] = [{"function_calls": [{"name": "decline", "arguments": {"reason": "x"}}]}] * 2
        response = httpx.post(f"{app.base}/api/proposals", json={"request": "lamp on in the attic"}, timeout=60)
        assert response.status_code == 422 and claude_sim.requests == []
    finally:
        app.close()


def test_without_any_key_drafts_are_unavailable_not_sent_anywhere(settings, claude_sim):
    app = serve(settings, anthropic_api_key="")
    try:
        assert status(app.base)["orchestrator"]["effective"] == "none"
        response = httpx.post(f"{app.base}/api/proposals", json={"request": "lamp on"}, timeout=30)
        assert response.status_code == 503 and response.json()["error"] == "unavailable"
        assert claude_sim.requests == []
    finally:
        app.close()


def test_the_orchestrator_searches_before_it_may_decline(base, claude_sim):
    """Owner, 2026-11-13: "lamp at my desk" was declined twice ("no sensor for sitting at the desk")
    because every binary_sensor was T3 and sorted behind 80 devices; the radar zones were never shown."""
    decline = {"tool_use": [{"name": "decline", "input": {"reason": "no desk sensor"}}]}
    search = {"tool_use": [{"name": "find_entities", "input": {"query": "motion", "area": ""}}]}
    claude_sim.script[:] = [decline, search, decline]
    response = httpx.post(
        f"{base}/api/proposals", json={"request": "Лампа над столом, когда я за столом"}, timeout=30
    )
    assert response.status_code == 422
    tools = [b["content"] for m in claude_sim.requests[2]["body"]["messages"] if isinstance(m["content"], list)
             for b in m["content"] if b.get("type") == "tool_result"]  # fmt: skip
    assert "Search with find_entities first" in tools[0], "the first decline is sent back"
    assert "binary_sensor.living_room_motion" in tools[1], "the search finds sensors"
    run = httpx.get(f"{base}/api/runs", params={"agent": "orchestrator"}).json()[0]
    trace = httpx.get(f"{base}/api/runs/{run['id']}").json()["trace"]
    assert [s["data"]["name"] for s in trace if s["type"] == "tool"] == ["find_entities"]


def test_the_owner_picks_the_model_revises_and_asks_what_it_does(settings, openai_cloud, claude_sim):
    """The setting says `auto` (Claude first); the owner picks OpenAI for one request, then asks
    Claude to change the draft in words (edited in place), then asks what it does."""
    app = serve(settings, orchestrator_provider="auto", openai_api_key=openai_cloud.sim.api_key,
                openai_base_url=openai_cloud.url + "/v1")  # fmt: skip
    try:
        orch = status(app.base)["orchestrator"]
        assert orch["choices"] == {"anthropic": True, "openai": True}
        submit = {"name": "submit_automation", "arguments": {"title": "Lamp on motion", "rationale": "r",
                                                             "expected_triggers": "motion", "yaml": GOOD}}  # fmt: skip
        openai_cloud.sim.script[:] = [{"function_calls": [submit]}, {"text": "Done."}]
        created = httpx.post(f"{app.base}/api/proposals", json={"request": "lamp at motion", "provider": "openai"},
                             timeout=30)  # fmt: skip
        assert created.status_code == 201, created.text
        proposal = created.json()
        assert httpx.get(f"{app.base}/api/runs/{proposal['run_id']}").json()["provider"] == "openai"
        assert claude_sim.requests == [], "the owner's pick, not the setting's Claude-first"

        night = GOOD.replace("actions:", "conditions:\n  - condition: sun\n    after: sunset\nactions:")
        claude_sim.script[:] = [
            {"tool_use": [{"name": "submit_automation", "input": {**submit["arguments"], "yaml": night}}]},
            {"text": "Changed."},
        ]
        revised = httpx.post(f"{app.base}/api/proposals/{proposal['id']}/revise",
                             json={"instruction": "only after sunset", "provider": "anthropic"}, timeout=30)  # fmt: skip
        assert revised.status_code == 200, revised.text
        validate(revised.json(), "/api/proposals/{proposal_id}/revise", "post")
        assert revised.json()["id"] == proposal["id"] and "sunset" in revised.json()["yaml"]
        asked = str(claude_sim.requests[0]["body"])
        assert "only after sunset" in asked and "light.living_room_lamp" in asked, "the current YAML is sent"

        before = len(claude_sim.requests)
        nothing = httpx.post(f"{app.base}/api/proposals/{proposal['id']}/explain",
                             json={"language": "ru", "cached_only": True}, timeout=30).json()  # fmt: skip
        assert nothing["text"] == "" and len(claude_sim.requests) == before, "no model call"
        claude_sim.requests.clear()
        claude_sim.script[:] = [{"text": "When motion is seen after sunset, the lamp goes on."}]
        explained = httpx.post(f"{app.base}/api/proposals/{proposal['id']}/explain",
                               json={"provider": "anthropic", "language": "ru"}, timeout=30)  # fmt: skip
        assert explained.status_code == 200, explained.text
        validate(explained.json(), "/api/proposals/{proposal_id}/explain", "post")
        assert explained.json()["text"].startswith("When motion") and not explained.json()["cached"]
        again = httpx.post(f"{app.base}/api/proposals/{proposal['id']}/explain",
                           json={"provider": "anthropic", "language": "ru"}, timeout=30).json()  # fmt: skip
        assert again["cached"] and again["text"] == explained.json()["text"], "not paid twice"
        assert len(claude_sim.requests) == 1
        assert "Russian" in str(claude_sim.requests[-1]["body"])

        rejected = httpx.post(f"{app.base}/api/proposals/{proposal['id']}/reject", json={}).json()
        claude_sim.script[:] = [
            {"tool_use": [{"name": "submit_automation", "input": {**submit["arguments"], "yaml": GOOD}}]},
            {"text": "ok"},
        ]
        fresh = httpx.post(f"{app.base}/api/proposals/{rejected['id']}/revise",
                           json={"instruction": "any time again", "provider": "local"}, timeout=30).json()  # fmt: skip
        assert fresh["id"] != rejected["id"] and fresh["status"] == "draft", (
            "a rejected one starts a new draft; `local` (before 0.5.0) means the setting"
        )
    finally:
        app.close()


def test_the_flow_editor_converts_yaml_and_back(base):
    out = httpx.post(f"{base}/api/automation-yaml", json={"yaml": GOOD.replace("triggers:", "trigger:")})
    assert out.status_code == 200, out.text
    validate(out.json(), "/api/automation-yaml", "post")
    config = out.json()["config"]
    assert config["triggers"][0]["entity_id"] == "binary_sensor.living_room_motion", (
        "normalized to plural keys"
    )
    back = httpx.post(f"{base}/api/automation-yaml", json={"config": config}).json()
    assert "light.living_room_lamp" in back["yaml"]
    part = httpx.post(f"{base}/api/automation-yaml", json={"yaml": "delay: '00:01:00'", "part": True}).json()
    assert part["config"] == {"delay": "00:01:00"}, "one step stays one step"
    bad = httpx.post(f"{base}/api/automation-yaml", json={"yaml": "- a list"})
    assert bad.status_code == 400
