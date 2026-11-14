"""M5 agent council and history analysis, through the ingress prefix against fake Home Assistant:
the mode helper, ownership, the orchestrator switching the mode in shadow, the logbook scan
(habit and undone automation), a finding drafted into a proposal, and the daily review."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
import pytest

from .test_m1_skeleton import wait_for
from .test_m4_automations import serve, validate

MODE = "input_select.agent_house_mode"
HABIT_YAML = """alias: Hallway light on motion
triggers: [{trigger: state, entity_id: binary_sensor.hallway_motion, to: "on"}]
actions: [{action: light.turn_on, target: {entity_id: light.hallway}}]
"""
OURS_YAML = """alias: Bedroom lamp at dusk
triggers: [{trigger: state, entity_id: sun.sun, to: below_horizon}]
actions: [{action: light.turn_on, target: {entity_id: light.bedroom_lamp}}]
"""


@pytest.fixture
def app(settings):
    served = serve(settings)
    yield served
    served.close()


def test_mode_helper_ownership_and_auto_mode_in_shadow(app, ha_state):
    base = app.base
    council = httpx.get(f"{base}/api/council").json()
    validate(council, "/api/council")
    assert council["mode"] is None and not council["mode_entity_present"]
    created = httpx.post(f"{base}/api/council/mode-helper")
    assert created.status_code == 201 and created.json()["entity_id"] == MODE
    assert httpx.post(f"{base}/api/council/mode-helper").status_code == 409
    wait_for(lambda: httpx.get(f"{base}/api/council").json()["mode"] == "home")
    owner = httpx.get(f"{base}/api/council/owner/{MODE}").json()
    validate(owner, "/api/council/owner/{entity_id}")
    assert owner["owner"] == "orchestrator"
    assert httpx.get(f"{base}/api/council/owner/light.nope").status_code == 404

    agent = {"id": "hallway", "name": "Hallway", "room": "hallway"}
    assert httpx.post(f"{base}/api/agents", json=agent).status_code == 201
    assert httpx.get(f"{base}/api/council/owner/light.hallway").json()["owner"] == "hallway"
    reserved = httpx.post(f"{base}/api/agents", json={**agent, "id": "orchestrator"})
    assert reserved.status_code in (400, 422) and "reserved" in reserved.text

    assert httpx.put(f"{base}/api/settings", json={"auto_house_mode": True}).status_code == 200
    ha_state.set_state("person.owner", "not_home")
    wait_for(lambda: httpx.get(f"{base}/api/actions", params={"agent": "orchestrator"}).json())
    [action] = httpx.get(f"{base}/api/actions", params={"agent": "orchestrator"}).json()
    assert action["verdict"] == "shadow" and action["data"] == {"option": "away"} and action["tier"] == "T1"
    assert ha_state.states[MODE]["state"] == "home", "shadow: the helper is not touched"

    # the owner switches the mode in HA: the room agent gets a task, its answer lands on the board
    ha_state.set_state(MODE, "away", ha_state.states[MODE]["attributes"])
    wait_for(lambda: any(r["kind"] == "report" for r in httpx.get(f"{base}/api/council").json()["board"]), 20)
    runs = httpx.get(f"{base}/api/runs", params={"agent": "hallway"}).json()
    assert runs[0]["wake_source"] == "task" and "away" in runs[0]["wake_reason"]


def test_logbook_scan_finds_a_habit_and_an_undone_automation_and_drafts_one(app, ha_state, llm_sim):
    base = app.base
    now = datetime.now(UTC).replace(microsecond=0)
    for back in range(1, 7):  # six evenings: motion, then the hallway light by the wall switch
        moment = now - timedelta(days=back, hours=1)
        ha_state.physical_change("binary_sensor.hallway_motion", "on", ts=moment)
        ha_state.physical_change("light.hallway", "on", ts=moment + timedelta(seconds=20))
        ha_state.physical_change("binary_sensor.hallway_motion", "off", ts=moment + timedelta(minutes=5))
        ha_state.physical_change("light.hallway", "off", ts=moment + timedelta(minutes=6))

    created = httpx.post(f"{base}/api/proposals", json={"yaml": OURS_YAML}).json()
    assert httpx.post(f"{base}/api/proposals/{created['id']}/approve").json()["status"] == "deployed"
    entity = "automation.bedroom_lamp_at_dusk"
    wait_for(lambda: entity in ha_state.states)
    time.sleep(0.05)
    for index in range(4):  # ours switches the lamp on; three times the owner turns it off again
        ha_state.run_automation(entity, [("light.bedroom_lamp", "on")])
        if index < 3:
            ha_state.user_change("light.bedroom_lamp", "off")
        time.sleep(0.01)

    scan = httpx.post(f"{base}/api/council/scan", timeout=30).json()
    validate(scan, "/api/council/scan", "post")
    kinds = {f["kind"]: f for f in scan["findings"]}
    assert set(kinds) == {"habit", "undo"}, scan
    habit, undo = kinds["habit"], kinds["undo"]
    habit = next(f for f in scan["findings"] if f["id"] == "habit:light.hallway")
    on = next(s for s in habit["data"]["states"] if s["state"] == "on")
    assert on["after"][0]["signal"] == "binary_sensor.hallway_motion" and on["days"] == 6
    assert scan["habits"]["dossiers"] >= 1 and "binary_sensor.hallway_motion" in habit["request"]
    assert undo["ha_id"] == "agent_house_bedroom_lamp_at_dusk" and undo["data"]["undone"] == 3
    assert [f["ha_id"] for f in scan["feedback"]] == ["agent_house_bedroom_lamp_at_dusk"]
    assert not any("agent" == f["data"].get("cause") for f in scan["findings"])

    submit = {"name": "submit_automation", "arguments": {"title": "Hallway light on motion",
              "rationale": "habit", "expected_triggers": "motion", "yaml": HABIT_YAML}}  # fmt: skip
    llm_sim.script[:] = [{"tool_calls": [submit]}, {"text": "Submitted."}]
    quoted = f"{base}/api/findings/{quote(habit['id'], safe='')}"
    response = httpx.post(f"{quoted}/draft")
    assert response.status_code == 202, response.text
    wait_for(lambda: httpx.get(f"{base}/api/findings", params={"status": "drafted"}).json(), 20)
    [drafted] = httpx.get(f"{base}/api/findings", params={"status": "drafted"}).json()
    validate([drafted], "/api/findings")
    proposal = httpx.get(f"{base}/api/proposals/{drafted['proposal_id']}").json()
    assert proposal["status"] == "draft" and "→ on 6× on 6 days" in proposal["rationale"]
    assert proposal["evidence"]["would_fire"] == 6
    assert httpx.post(f"{quoted}/draft").status_code == 409, "a finding is drafted once"
    dismissed = httpx.post(f"{base}/api/findings/{quote(undo['id'], safe='')}/dismiss").json()
    assert dismissed["status"] == "dismissed"


def test_daily_review_reports_to_the_inbox(app, llm_sim):
    base = app.base
    llm_sim.script[:] = [{"text": "Calm day, nothing to fix."}]
    assert httpx.post(f"{base}/api/council/review").status_code == 202
    wait_for(lambda: (httpx.get(f"{base}/api/council").json()["review"]["last"] or {}).get("summary"), 20)
    review = httpx.get(f"{base}/api/council").json()["review"]["last"]
    assert review["summary"].startswith("Calm day")
    inbox = httpx.get(f"{base}/api/inbox").json()
    assert any(i["agent_id"] == "orchestrator" and "Daily review" in i["summary"] for i in inbox)


def test_manager_and_metrics_endpoints_follow_the_contract(app):
    base = app.base
    status = httpx.get(f"{base}/api/manager").json()
    validate(status, "/api/manager")
    assert status["enabled"] is False and status["mode"] == "shadow"
    assert (
        httpx.put(f"{base}/api/settings", json={"manager_enabled": True, "manager_sweep_min": 0}).status_code
        == 200
    )
    assert httpx.get(f"{base}/api/manager").json()["enabled"] is True
    woken = httpx.post(f"{base}/api/manager/wake", json={"reason": "tester"})
    assert woken.status_code == 202 and woken.json()["status"] in ("started", "queued")
    wait_for(lambda: any("manual: tester" in (r.get("wake_reason") or "")
                         for r in httpx.get(f"{base}/api/runs", params={"agent": "orchestrator"}).json()), 20)  # fmt: skip
    metrics = httpx.get(f"{base}/api/metrics", params={"days": 3}).json()
    validate(metrics, "/api/metrics")
    assert metrics["days"] == 3 and metrics["program"]["score"] is None  # nothing deployed yet
    assert httpx.put(f"{base}/api/kill-switch", json={"engaged": True}).status_code == 200
    assert httpx.post(f"{base}/api/manager/wake", json={}).status_code == 409


def test_metrics_match_the_known_outcomes_of_a_played_history(app, ha_state):
    """Ground truth from the sim. Five rounds: ours switches the lamp on; in rounds 1 and 3 the owner
    switches it off by hand (undone), otherwise ours switches it off again (both changes kept).
    A run that changes nothing leaves no logbook entry and is no outcome."""
    base = app.base
    created = httpx.post(f"{base}/api/proposals", json={"yaml": OURS_YAML}).json()
    assert httpx.post(f"{base}/api/proposals/{created['id']}/approve").json()["status"] == "deployed"
    entity = "automation.bedroom_lamp_at_dusk"
    wait_for(lambda: entity in ha_state.states)
    time.sleep(0.05)
    for index in range(5):
        ha_state.run_automation(entity, [("light.bedroom_lamp", "on")])
        if index in (1, 3):
            ha_state.user_change("light.bedroom_lamp", "off")
        else:
            ha_state.run_automation(entity, [("light.bedroom_lamp", "off")])
        time.sleep(0.01)
    metrics = httpx.get(f"{base}/api/metrics", params={"days": 1}).json()
    validate(metrics, "/api/metrics")
    [item] = metrics["automations"]
    assert (item["kept"], item["undone"], item["unobserved"]) == (6, 2, 0), item
    assert item["precision"]["value"] == round(6 / 8, 3)
    assert item["precision"]["low"] < item["precision"]["value"] < item["precision"]["high"]
    assert metrics["program"]["counts"]["kept"] == 6 and metrics["program"]["counts"]["undone"] == 2
