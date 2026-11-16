"""M5 chat and config export / import, through the ingress prefix: a chat turn is a traced run of the
orchestrator, which only reads and drafts (D40: there are no room agents to chat with), and an
imported config is validated as a whole; an export from before 0.5.0 imports, its agents ignored."""

from __future__ import annotations

import httpx
import yaml

from .test_m4_automations import GOOD, base, validate  # noqa: F401 - `base` is a fixture


def ask(base, target, message, session_id=None):  # noqa: F811
    body = {"target": target, "message": message, **({"session_id": session_id} if session_id else {})}
    return httpx.post(f"{base}/api/chat", json=body, timeout=30)


def test_chat_with_the_orchestrator_reads_and_drafts(base, claude_sim, ha_state):  # noqa: F811
    claude_sim.script[:] = [
        {"tool_use": [{"name": "find_entities", "input": {"query": "lamp", "area": "living_room"}}]},
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
    tools = [t["name"] for t in claude_sim.requests[0]["body"]["tools"]]
    assert tools == ["find_entities", "submit_automation"], "the orchestrator has no way to actuate"
    assert "light.living_room_lamp" in str(claude_sim.requests[1]["body"]["messages"][-1]["content"])

    claude_sim.script[:] = [
        {"tool_use": [{"name": "submit_automation", "input": {
            "title": "Lamp on motion", "rationale": "asked in chat", "expected_triggers": "motion",
            "yaml": GOOD}}]},
        {"text": "Drafted; review it on the Proposals page."},
    ]  # fmt: skip
    claude_sim.requests.clear()
    second = httpx.post(f"{base}/api/chat", json={"message": "Switch it on whenever someone walks in",
                                                   "session_id": answer["session_id"]}, timeout=30).json()  # fmt: skip
    assert second["session_id"] == answer["session_id"], "`target` may be left out"
    prompt = claude_sim.requests[0]["body"]["messages"][0]["content"]
    assert "Earlier in this chat" in prompt and "Is the living room lamp on?" in prompt
    proposals = httpx.get(f"{base}/api/proposals").json()
    assert [p["status"] for p in proposals] == ["draft"] and proposals[0]["run_id"] == second["run"]["id"]
    assert ha_state.config_writes == [] and ha_state.service_calls == []


def test_only_the_orchestrator_answers_and_the_kill_switch_stops_it(base, claude_sim):  # noqa: F811
    for target in ("living", "nobody", "learner"):
        refused = ask(base, target, "hi")
        assert refused.status_code == 404 and "only the orchestrator" in refused.json()["detail"]
    assert httpx.post(f"{base}/api/chat", json={"target": "orchestrator"}).status_code == 400
    httpx.put(f"{base}/api/kill-switch", json={"engaged": True})
    refused = ask(base, "orchestrator", "hi")
    assert refused.status_code == 409 and refused.json()["error"] == "kill_switch"
    assert claude_sim.requests == []


def test_config_export_and_import(base, settings):  # noqa: F811
    httpx.put(f"{base}/api/settings", json={"habit_days": 21})
    exported = httpx.get(f"{base}/api/config/export")
    assert exported.status_code == 200 and exported.headers["content-type"].startswith("text/yaml")
    document = yaml.safe_load(exported.text)
    assert set(document) == {"agent_house_export", "policy", "settings"}
    assert document["settings"]["habit_days"] == 21 and "domain_tiers" in document["policy"]
    for secret in (settings.ha_token, settings.jetson_token, "api_key", "ha_url"):
        assert secret not in exported.text, "add-on options (and so every secret) stay out"

    def post(doc):
        return httpx.post(
            f"{base}/api/config/import", content=yaml.safe_dump(doc), headers={"Content-Type": "text/yaml"}
        )

    assert post({"agents": []}).status_code == 422, "not an export"
    assert post({**document, "settings": {"nope": 1}}).status_code == 422
    assert post({**document, "rooms": []}).status_code == 422
    assert httpx.get(f"{base}/api/settings").json()["runtime"]["habit_days"] == 21, "nothing was applied"

    document["settings"]["habit_days"] = 12
    document["policy"]["entity_tiers"] = {"light.kitchen_counter": "T2"}
    imported = post(document)
    assert imported.status_code == 200, imported.text
    validate(imported.json(), "/api/config/import", "post")
    assert imported.json() == {"policy": True, "settings": True, "ignored": []}
    assert httpx.get(f"{base}/api/settings").json()["runtime"]["habit_days"] == 12
    assert httpx.get(f"{base}/api/policy").json()["entity_tiers"] == {"light.kitchen_counter": "T2"}


def test_an_export_from_before_0_5_0_imports_with_its_agents_ignored(base):  # noqa: F811
    old = {
        "agent_house_export": 1,
        "agents": [{"id": "living", "name": "Living room", "room": "living_room", "mode": "live"}],
        "policy": httpx.get(f"{base}/api/policy").json(),
        "settings": {"llm_max_retries": 4, "llm_timeout_s": 300, "learner_mode": "shadow", "habit_days": 9},
        "active_profile": "qwen35-4b-balanced",
    }
    imported = httpx.post(f"{base}/api/config/import", content=yaml.safe_dump(old),
                          headers={"Content-Type": "text/yaml"})  # fmt: skip
    assert imported.status_code == 200, imported.text
    assert imported.json() == {"policy": True, "settings": True, "ignored": ["agents", "active_profile"]}
    runtime = httpx.get(f"{base}/api/settings").json()["runtime"]
    assert runtime["habit_days"] == 9 and not any(k.startswith("llm_") for k in runtime)
    assert httpx.get(f"{base}/api/agents").status_code == 404
