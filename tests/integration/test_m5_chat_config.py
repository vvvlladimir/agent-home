"""M5 chat and config export / import, through the ingress prefix: a chat turn is a normal traced
run (room agents keep their policy and shadow mode, the orchestrator only reads and drafts), and an
imported config is validated as a whole and never switches an agent to live."""

from __future__ import annotations

import httpx
import yaml

from .test_m4_automations import GOOD, base, validate  # noqa: F401 - `base` is a fixture

AGENT = {"id": "living", "name": "Living room", "room": "living_room"}


def ask(base, target, message, session_id=None):  # noqa: F811
    body = {"target": target, "message": message, **({"session_id": session_id} if session_id else {})}
    return httpx.post(f"{base}/api/chat", json=body, timeout=30)


def test_chat_with_the_orchestrator_reads_and_drafts(base, llm_sim, ha_state):  # noqa: F811
    llm_sim.script[:] = [
        {"tool_calls": [{"name": "find_entities", "arguments": {"query": "lamp", "area": "living_room"}}]},
        {"text": "The living room lamp is on."},
    ]
    first = ask(base, "orchestrator", "Is the living room lamp on?")
    assert first.status_code == 200, first.text
    answer = first.json()
    validate(answer, "/api/chat", "post")
    assert answer["reply"] == "The living room lamp is on."
    run = answer["run"]
    assert (run["agent_id"], run["wake_source"], run["status"]) == ("orchestrator", "chat", "ok")
    assert [s["data"]["name"] for s in run["trace"] if s["type"] == "tool"] == ["find_entities"]
    tools = [t["function"]["name"] for t in llm_sim.requests[0]["tools"]]
    assert tools == ["find_entities", "submit_automation"], "the orchestrator has no way to actuate"
    assert "light.living_room_lamp" in llm_sim.requests[1]["messages"][-1]["content"]

    llm_sim.script[:] = [
        {"tool_calls": [{"name": "submit_automation", "arguments": {
            "title": "Lamp on motion", "rationale": "asked in chat", "expected_triggers": "motion",
            "yaml": GOOD}}]},
        {"text": "Drafted; review it on the Proposals page."},
    ]  # fmt: skip
    llm_sim.requests.clear()
    second = ask(base, "orchestrator", "Switch it on whenever someone walks in", answer["session_id"]).json()
    assert second["session_id"] == answer["session_id"]
    prompt = llm_sim.requests[0]["messages"][1]["content"]
    assert "Earlier in this chat" in prompt and "Is the living room lamp on?" in prompt
    proposals = httpx.get(f"{base}/api/proposals").json()
    assert [p["status"] for p in proposals] == ["draft"] and proposals[0]["run_id"] == second["run"]["id"]
    assert ha_state.config_writes == [] and ha_state.service_calls == []


def test_chat_with_a_room_agent_is_a_normal_shadow_run(base, llm_sim, ha_state):  # noqa: F811
    assert httpx.post(f"{base}/api/agents", json=AGENT).status_code == 201
    llm_sim.script[:] = [
        {"tool_calls": [{"name": "call_service", "arguments": {
            "domain": "light", "service": "turn_off", "entity_id": "light.living_room_lamp"}}]},
        {"text": "Done: the lamp is off."},
    ]  # fmt: skip
    response = ask(base, "living", "Выключи лампу")
    assert response.status_code == 200, response.text
    answer = response.json()
    validate(answer, "/api/chat", "post")
    run = answer["run"]
    assert answer["reply"] == "Done: the lamp is off." and run["wake_source"] == "chat" and run["shadow"]
    assert "Owner: Выключи лампу" in run["trace"][0]["data"]["user"]
    assert [s["data"]["verdict"] for s in run["trace"] if s["type"] == "policy"] == ["shadow"]
    assert ha_state.service_calls == [], "shadow mode holds in chat too"

    assert ask(base, "nobody", "hi").status_code == 404
    assert httpx.post(f"{base}/api/chat", json={"target": "living"}).status_code == 400
    httpx.put(f"{base}/api/kill-switch", json={"engaged": True})
    for target in ("living", "orchestrator"):
        refused = ask(base, target, "hi")
        assert refused.status_code == 409 and refused.json()["error"] == "kill_switch"


def test_config_export_and_import(base, settings):  # noqa: F811
    httpx.post(f"{base}/api/agents", json=AGENT)
    httpx.put(f"{base}/api/settings", json={"llm_max_retries": 4})
    exported = httpx.get(f"{base}/api/config/export")
    assert exported.status_code == 200 and exported.headers["content-type"].startswith("text/yaml")
    document = yaml.safe_load(exported.text)
    assert [a["id"] for a in document["agents"]] == ["living"] and document["settings"][
        "llm_max_retries"
    ] == 4
    assert "domain_tiers" in document["policy"]
    for secret in (settings.ha_token, settings.jetson_token, "api_key", "ha_url"):
        assert secret not in exported.text, "add-on options (and so every secret) stay out"

    def post(doc):
        return httpx.post(
            f"{base}/api/config/import", content=yaml.safe_dump(doc), headers={"Content-Type": "text/yaml"}
        )

    bad = {**document, "agents": [*document["agents"], {"id": "Bad Id", "name": "x", "room": "kitchen"}]}
    refused = post(bad)
    assert refused.status_code == 422 and "agents.1" in refused.json()["detail"]
    assert post({"agents": []}).status_code == 422, "not an export"
    assert post({**document, "settings": {"nope": 1}}).status_code == 422
    assert [a["id"] for a in httpx.get(f"{base}/api/agents").json()] == ["living"], "nothing was applied"

    document["agents"][0]["mode"] = "live"
    document["agents"].append({"id": "kitchen", "name": "Kitchen", "room": "kitchen", "mode": "live"})
    document["settings"]["llm_max_retries"] = 1
    document["policy"]["entity_tiers"] = {"light.kitchen_counter": "T2"}
    imported = post(document)
    assert imported.status_code == 200, imported.text
    assert imported.json() == {
        "agents": ["living", "kitchen"],
        "set_to_shadow": ["living", "kitchen"],
        "policy": True,
        "settings": True,
    }
    agents = {a["id"]: a for a in httpx.get(f"{base}/api/agents").json()}
    assert {a["mode"] for a in agents.values()} == {"shadow"}, "a file never switches an agent to live"
    assert httpx.get(f"{base}/api/settings").json()["runtime"]["llm_max_retries"] == 1
    assert httpx.get(f"{base}/api/policy").json()["entity_tiers"] == {"light.kitchen_counter": "T2"}
