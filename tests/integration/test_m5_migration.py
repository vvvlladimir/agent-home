"""M5 migration, through the ingress prefix against fake Home Assistant: the importer only reads,
the replacement is a normal proposal, parity replays both against the same history, and "disable
original" is an explicit, reversible switch that never edits the owner's automation."""

from __future__ import annotations

import copy
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from .test_m1_skeleton import wait_for
from .test_m4_automations import serve, validate

SAMPLES = Path(__file__).resolve().parents[2] / "owner-reports" / "nodered"
OWN = "auto_1700000000001"  # fake HA's "Hallway motion light"
REPLACEMENT = """
alias: Hallway light on motion
triggers:
  - trigger: state
    entity_id: binary_sensor.hallway_motion
    to: "on"
actions:
  - action: light.turn_on
    target: {entity_id: light.hallway}
"""


@pytest.fixture
def app(settings, tmp_path, ha_state):
    share = tmp_path / "share"
    (share / "nodered").mkdir(parents=True)
    shutil.copy(SAMPLES / "bedroom.json", share / "nodered" / "bedroom.json")
    (share / "nodered" / "broken.json").write_text("{")
    ha_state.put_script(
        "goodnight",
        {"alias": "Good night", "sequence": [{"service": "light.turn_off", "entity_id": "light.hallway"}]},
    )
    served = serve(settings, share_dir=share)
    yield served
    served.close()


def test_import_propose_parity_and_reversible_disable(app, ha_state, llm_sim):
    base = app.base
    owners = copy.deepcopy(ha_state.automations)
    report = httpx.post(f"{base}/api/migration/import").json()
    validate(report, "/api/migration/import", "post")
    assert report["imported"] == report["total"] >= 7 and report["updated"] == 0
    assert any(s.startswith("broken.json") for s in report["skipped"])
    originals = httpx.get(f"{base}/api/migration/originals").json()
    validate(originals, "/api/migration/originals")
    by_id = {o["id"]: o for o in originals}
    assert {o["source"] for o in originals} == {"ha_automation", "ha_script", "node_red"}
    assert by_id[OWN]["enabled"] is True and by_id[OWN]["entity_id"] == "automation.hallway_motion_light"
    assert (
        by_id[OWN]["summary"] == "When: binary_sensor.hallway_motion → on. Does: light.turn_on light.hallway"
    )
    assert by_id["script_goodnight"]["summary"] == "Does: light.turn_off light.hallway"
    assert httpx.post(f"{base}/api/migration/import").json()["imported"] == 0, "a second import adds nothing"
    assert ha_state.config_writes == [] and ha_state.service_calls == [], "the importer only reads"

    # the orchestrator drafts the replacement: a normal proposal that names the original
    assert httpx.get(f"{base}/api/migration/originals/{OWN}/parity").status_code == 409
    llm_sim.script[:] = [
        {"tool_calls": [{"name": "submit_automation", "arguments": {
            "title": "Hallway light on motion", "rationale": "same as the original",
            "expected_triggers": "hallway motion", "yaml": REPLACEMENT}}]},
        {"text": "Submitted."},
    ]  # fmt: skip
    queued = httpx.post(f"{base}/api/migration/originals/{OWN}/propose")
    assert queued.status_code == 202
    run_id = queued.json()["run_id"]
    original = wait_for(
        lambda: (
            (o := {x["id"]: x for x in httpx.get(f"{base}/api/migration/originals").json()}[OWN])[
                "replacement_proposal_id"
            ]
            and o
        )
    )
    proposal = httpx.get(f"{base}/api/proposals/{original['replacement_proposal_id']}").json()
    assert proposal["status"] == "draft" and proposal["replaces_original"] == OWN
    assert proposal["run_id"] == run_id
    assert "Hallway motion light" in str(llm_sim.requests[0]["messages"][1]), "the request names the original"
    assert original["replacement_ha_id"] is None, "not deployed yet"

    # parity: both replayed against the same history
    start = datetime.now(UTC) - timedelta(hours=5)
    for hour, state in enumerate(["on", "off", "on", "off"]):
        ha_state.set_state("binary_sensor.hallway_motion", state, ts=start + timedelta(hours=hour))
    parity = httpx.get(f"{base}/api/migration/originals/{OWN}/parity", params={"days": 1}).json()
    validate(parity, "/api/migration/originals/{original_id}/parity")
    assert (parity["original_fired"], parity["replacement_would_fire"], parity["matched"]) == (2, 2, 2)
    assert parity["supported"] is True and parity["diffs"] == []

    # deploy the replacement, then switch the original off, and on again
    assert httpx.post(f"{base}/api/proposals/{proposal['id']}/approve").status_code == 200
    off = httpx.put(f"{base}/api/migration/originals/{OWN}/enabled", json={"enabled": False})
    assert off.status_code == 200, off.text
    validate(off.json(), "/api/migration/originals/{original_id}/enabled", "put")
    assert off.json()["enabled"] is False and off.json()["disabled_by_us"] is True
    assert off.json()["replacement_ha_id"] == proposal["ha_id"]
    wait_for(lambda: ha_state.states["automation.hallway_motion_light"]["state"] == "off")
    assert ha_state.service_calls[-1]["service"] == "turn_off"
    again = httpx.post(f"{base}/api/migration/import").json()
    assert again["imported"] == 0, "our own deployed automation is not imported as an original"
    on = httpx.put(f"{base}/api/migration/originals/{OWN}/enabled", json={"enabled": True}).json()
    assert on["enabled"] is True and on["disabled_by_us"] is False
    wait_for(lambda: ha_state.states["automation.hallway_motion_light"]["state"] == "on")

    assert all(ha_state.automations[k] == v for k, v in owners.items()), "never edited, never deleted"
    assert all(w["id"].startswith("agent_house_") for w in ha_state.config_writes)
    assert {c["domain"] for c in ha_state.service_calls} == {"automation"}


def test_what_cannot_be_switched_and_the_kill_switch(app, ha_state):
    base = app.base
    httpx.post(f"{base}/api/migration/import")
    originals = httpx.get(f"{base}/api/migration/originals").json()
    flow = next(o for o in originals if o["source"] == "node_red")
    for other in ("script_goodnight", flow["id"]):
        refused = httpx.put(f"{base}/api/migration/originals/{other}/enabled", json={"enabled": False})
        assert refused.status_code == 409, refused.text
    assert (
        httpx.put(f"{base}/api/migration/originals/nope/enabled", json={"enabled": False}).status_code == 404
    )
    assert httpx.put(f"{base}/api/migration/originals/{OWN}/enabled", json={}).status_code == 400

    httpx.put(f"{base}/api/kill-switch", json={"engaged": True, "reason": "test"})
    for call in (
        httpx.put(f"{base}/api/migration/originals/{OWN}/enabled", json={"enabled": False}),
        httpx.post(f"{base}/api/migration/originals/{OWN}/propose"),
    ):
        assert call.status_code == 409 and call.json()["error"] == "kill_switch"
    assert ha_state.service_calls == []
    undo = httpx.put(f"{base}/api/migration/originals/{OWN}/enabled", json={"enabled": True})
    assert undo.status_code == 200, "enabling the owner's automation again is always possible"


def test_a_replacement_with_the_originals_alias_does_not_take_its_entity(app, ha_state, llm_sim):
    """QA-003: HA gives a second automation with the same alias `<slug>_2`; the original keeps its
    entity, stays the owner's, and can still be switched off from the migration page."""
    base = app.base
    httpx.post(f"{base}/api/migration/import")
    same = REPLACEMENT.replace("alias: Hallway light on motion", "alias: Hallway motion light")
    llm_sim.script[:] = [
        {"tool_calls": [{"name": "submit_automation", "arguments": {
            "title": "Hallway motion light", "rationale": "same", "expected_triggers": "motion",
            "yaml": same}}]},
        {"text": "Submitted."},
    ]  # fmt: skip
    assert httpx.post(f"{base}/api/migration/originals/{OWN}/propose").status_code == 202
    pid = wait_for(
        lambda: {o["id"]: o for o in httpx.get(f"{base}/api/migration/originals").json()}[OWN][
            "replacement_proposal_id"
        ]
    )
    assert httpx.post(f"{base}/api/proposals/{pid}/approve").status_code == 200
    ours = next(
        e for e, s in ha_state.states.items() if s["attributes"].get("id", "").startswith("agent_house_")
    )
    assert ours == "automation.hallway_motion_light_2"
    assert ha_state.states["automation.hallway_motion_light"]["attributes"]["id"] == "1700000000001"
    off = httpx.put(f"{base}/api/migration/originals/{OWN}/enabled", json={"enabled": False})
    assert off.status_code == 200, off.text
    assert off.json()["entity_id"] == "automation.hallway_motion_light"
    wait_for(lambda: ha_state.states["automation.hallway_motion_light"]["state"] == "off")
    assert ha_state.states[ours]["state"] == "on", "ours is untouched"


def test_parity_after_a_rejected_replacement_says_so(app, ha_state, llm_sim):
    """QA-004: the 409 after a rejection names the rejected draft instead of "ask for one first"."""
    base = app.base
    httpx.post(f"{base}/api/migration/import")
    llm_sim.script[:] = [
        {"tool_calls": [{"name": "submit_automation", "arguments": {
            "title": "Hallway light on motion", "rationale": "same", "expected_triggers": "motion",
            "yaml": REPLACEMENT}}]},
        {"text": "Submitted."},
    ]  # fmt: skip
    httpx.post(f"{base}/api/migration/originals/{OWN}/propose")
    pid = wait_for(
        lambda: {o["id"]: o for o in httpx.get(f"{base}/api/migration/originals").json()}[OWN][
            "replacement_proposal_id"
        ]
    )
    assert httpx.post(f"{base}/api/proposals/{pid}/reject", json={}).status_code == 200
    for _ in range(2):
        parity = httpx.get(f"{base}/api/migration/originals/{OWN}/parity")
        assert parity.status_code == 409
        assert f"{pid} was rejected" in parity.json()["detail"]
