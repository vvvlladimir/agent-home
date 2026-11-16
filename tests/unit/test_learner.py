"""The Jetson learner (D38, jetson/learner/): journal, training layers, live suggestions, replay and
its HTTP API with the bearer token. Hermetic: an in-memory house, a server on an ephemeral port."""

from __future__ import annotations

import json
import random
import sys
import threading
from pathlib import Path

import httpx
import jsonschema
import pytest

from app.core.insights import Change
from tests.unit.predict_house import AREAS, at, household

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "jetson"))

from learner import replay  # noqa: E402
from learner.journal import Journal  # noqa: E402
from learner.server import Learner, serve  # noqa: E402

SCHEMAS = ROOT / "contracts" / "schemas"
DAYS = 21
CEILING, RADAR = "light.living_ceiling", "binary_sensor.living_radar"
KITCHEN_RADAR = "binary_sensor.kitchen_radar"
NODE_RED = "user:Node-RED"


def schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text())


def controller(days: int = DAYS, seed: int = 7, timeout_s: float = 120.0) -> list[Change]:
    """Node-RED switches the living-room ceiling light on with the radar in the dark and off when the
    radar has been quiet for `timeout_s`. The radar drops out for 1–5 min while someone sits still;
    when that switches the light off, the person switches it back on by hand. They also leave the room
    for real (the kitchen's radar sees them at once)."""
    rng = random.Random(seed)
    out: list[Change] = []
    for d in range(days):
        t, end = at(d, 18.0 + rng.uniform(0, 0.3)), at(d, 22.8)
        out.append(Change(t, RADAR, "on", "physical"))
        out.append(Change(t + 2, CEILING, "on", "automation", NODE_RED))
        while t < end:
            t += rng.uniform(900, 1800)
            if rng.random() < 0.3:  # a real walk out: the kitchen sees it, back after 10–20 min
                length = rng.uniform(600, 1200)
                out.append(Change(t + 5, KITCHEN_RADAR, "on", "physical"))
                out.append(Change(t + length - 30, KITCHEN_RADAR, "off", "physical"))
            else:  # sitting still: the radar drops out
                length = rng.uniform(60, 300)
            out.append(Change(t, RADAR, "off", "physical"))
            if length >= timeout_s:
                off = t + timeout_s
                out.append(Change(off, CEILING, "off", "automation", NODE_RED))
                if length < 600:  # still there: back on by hand
                    out.append(Change(off + rng.uniform(10, 40), CEILING, "on", "user"))
            t += length
            out.append(Change(t, RADAR, "on", "physical"))
            if length >= 600:
                out.append(Change(t + 2, CEILING, "on", "automation", NODE_RED))
        out.append(Change(end, RADAR, "off", "physical"))
        out.append(Change(end + timeout_s, CEILING, "off", "automation", NODE_RED))
    return out


def registry_doc() -> dict:
    areas = {**AREAS, CEILING: "living", RADAR: "living", KITCHEN_RADAR: "kitchen"}
    classes = {
        "binary_sensor.hall_motion": "motion",
        "binary_sensor.living_motion": "motion",
        RADAR: "occupancy",
        KITCHEN_RADAR: "occupancy",
    }
    entities = [
        {"entity_id": e, "area_id": a, "state": "off", "attributes": {"device_class": classes.get(e)}}
        for e, a in areas.items()
    ]
    entities += [
        {"entity_id": "sun.sun", "state": "below_horizon"},
        {"entity_id": "person.owner", "state": "home"},
    ]
    return {
        "schema_version": 1, "ts": at(0, 0), "time_zone": None,
        "areas": [{"area_id": a, "name": a.title()} for a in sorted(set(areas.values()))],
        "entities": entities, "ours": {}, "policy": None,
        "settings": {"habit_days": 14, "habit_min_repeats": 3},
    }  # fmt: skip


def events(changes: list[Change]) -> list[dict]:
    return [
        {
            "ts": c.ts,
            "kind": "state",
            "entity_id": c.entity_id,
            "state": c.state,
            "cause": c.cause,
            "ref": c.ref,
        }
        for c in sorted(changes, key=lambda c: c.ts)
    ]


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    learner = Learner(tmp_path_factory.mktemp("learner"))
    learner.put_registry(registry_doc())
    learner.journal.add(events(household(DAYS) + controller()))
    doc = learner.train(now=at(DAYS, 0))
    return learner, doc


# --- journal ----------------------------------------------------------------------------------------
def test_journal_ignores_duplicates_and_reads_changes_like_the_logbook(tmp_path):
    journal = Journal(tmp_path / "j.db")
    batch = events(household(2))
    assert journal.add(batch) == (len(batch), 0)
    assert journal.add(batch[:10]) == (0, 10)  # a backfill overlapping what came live
    journal.add([{"ts": at(0, 9), "kind": "trigger", "entity_id": "automation.x"},
                 {"ts": at(0, 9.1), "kind": "answer", "entity_id": "event.ask", "state": "AGENT_HOUSE_X_NO"}])  # fmt: skip
    changes = journal.changes(at(0, 0), at(3, 0))
    assert len(changes) == len(batch) and changes[0].ts <= changes[-1].ts
    assert all(isinstance(c, Change) for c in changes)
    assert journal.fires(at(0, 0), at(1, 0)) == {"automation.x": [at(0, 9)]}
    assert journal.answers(at(0, 0), at(1, 0)) == [(at(0, 9.1), "AGENT_HOUSE_X_NO")]
    stats = journal.stats()
    assert stats["events"] == len(batch) + 2 and stats["last_ts"] == batch[-1]["ts"]
    assert journal.prune(1, now=at(2, 12)) > 0


# --- training ---------------------------------------------------------------------------------------
def test_the_house_model_follows_its_contract(trained):
    _, doc = trained
    jsonschema.validate(json.loads(json.dumps(doc)), schema("house_model.schema.json"))
    assert doc["history"]["days"] == pytest.approx(DAYS, abs=1)


def test_habits_are_the_d35_predictions_over_the_longer_history(trained):
    _, doc = trained
    preds = {(p["entity_id"], p["state"]) for p in doc["habits"]["predictions"]}
    assert ("light.hallway", "on") in preds
    assert doc["habits"]["days"] >= DAYS - 1  # more than the add-on's 14 days of logbook


def test_live_models_have_an_out_of_sample_record(trained):
    _, doc = trained
    live = {(m["entity_id"], m["state"]): m for m in doc["live"]}
    hall = live[("light.hallway", "on")]
    assert hall["fires"] >= 4 and hall["right"] / hall["fires"] > 0.6 and hall["right_days"] >= 3


def test_a_live_suggestion_comes_when_the_context_says_so(trained):
    learner, _ = trained
    t = at(DAYS, 19.0)  # dark, nobody in the hall yet
    learner.post_events({"schema_version": 1, "events": [
        {"ts": t - 3600, "kind": "state", "entity_id": "light.hallway", "state": "off", "cause": "user"},
        {"ts": t - 1800, "kind": "state", "entity_id": "binary_sensor.hall_motion", "state": "off", "cause": "physical"},
    ]}, now=t)  # fmt: skip
    assert not [s for s in learner.now(t)["suggestions"] if s["entity_id"] == "light.hallway"]
    answer = learner.post_events({"schema_version": 1, "events": [
        {"ts": t, "kind": "state", "entity_id": "binary_sensor.hall_motion", "state": "on", "cause": "physical"},
    ]}, now=t + 5)  # fmt: skip
    hall = [s for s in answer["suggestions"] if (s["entity_id"], s["state"]) == ("light.hallway", "on")]
    assert hall and hall[0]["earned"] and hall[0]["mode"] in ("act", "ask") and hall[0]["p"] > 0.5
    jsonschema.validate(learner.now(t + 5), schema("learner_now.schema.json"))


def test_dependencies_and_routines(trained):
    _, doc = trained
    deps = {(d["after"]["entity_id"], d["after"]["state"], d["then"]["entity_id"], d["then"]["state"])
            for d in doc["dependencies"]}  # fmt: skip
    assert ("binary_sensor.hall_motion", "on", "light.hallway", "on") in deps
    kitchen = [
        r
        for r in doc["routines"]
        if (r["entity_id"], r["state"], r["days"]) == ("light.kitchen", "on", "workdays")
    ]
    hh, mm = (int(x) for x in kitchen[0]["at"].split(":"))
    assert abs(hh * 60 + mm - 405) <= 10 and kitchen[0]["spread_min"] < 10  # 06:45 ± 9 min in the house


def test_corrections_and_the_learnt_timeout_of_the_owners_light_controller(trained):
    _, doc = trained
    corrected = [
        c
        for c in doc["corrections"]
        if (c["source"], c["entity_id"], c["state"]) == (NODE_RED, CEILING, "off")
    ]
    assert corrected and corrected[0]["corrected"] >= 10
    timeout = [t for t in doc["timeouts"] if t["entity_id"] == CEILING]
    assert timeout, doc["timeouts"]
    t = timeout[0]
    assert t["source"] == NODE_RED and t["current_min"] == pytest.approx(2, abs=0.2)
    assert 5 <= t["suggested_min"] <= 10  # dropouts last up to 5 min; real walks out 10–20 min
    assert t["cost_suggested"] < t["cost_now"] and t["corrections_per_day"] > 0.5


def test_usual_states_and_event_relevance_cover_the_longer_history(trained):
    _, doc = trained
    assert "light.lamp" in doc["usual_states"]
    assert doc["event_relevance"]["binary_sensor.hall_motion|on"][1] > 50


# --- replay -----------------------------------------------------------------------------------------
def test_replay_counts_what_a_good_and_a_bad_automation_would_have_done(trained):
    learner, _ = trained
    good = {"triggers": [{"trigger": "state", "entity_id": "binary_sensor.hall_motion", "to": "on"}],
            "conditions": [{"condition": "state", "entity_id": "sun.sun", "state": "below_horizon"}],
            "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.hallway"}}]}  # fmt: skip
    bad = {"triggers": [{"trigger": "time", "at": "12:00:00"}],
           "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.hallway"}}]}  # fmt: skip
    g = replay.replay(learner.journal, good, days=14, now=at(DAYS, 0))
    b = replay.replay(learner.journal, bad, days=14, now=at(DAYS, 0))
    assert g["supported"] and b["supported"]
    assert g["outcomes"]["right"] > 3 * g["outcomes"]["wrong"]
    assert b["outcomes"]["right"] == 0 and b["fires"] == 14
    unsupported = replay.replay(
        learner.journal, {**good, "actions": [{"choose": []}]}, days=14, now=at(DAYS, 0)
    )
    assert not unsupported["supported"] and unsupported["unsupported"]


# --- the HTTP API -----------------------------------------------------------------------------------
@pytest.fixture
def api(tmp_path):
    learner = Learner(tmp_path)
    server = serve(learner, "127.0.0.1", 0, "secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    yield learner, url
    server.shutdown()
    learner.journal.close()


def test_api_needs_the_token_except_for_healthz(api):
    _, url = api
    assert httpx.get(url + "/healthz").json() == {"ok": True}
    for method, path in (("GET", "/status"), ("GET", "/model"), ("GET", "/now"), ("POST", "/events"),
                         ("PUT", "/registry"), ("POST", "/train"), ("POST", "/replay")):  # fmt: skip
        assert httpx.request(method, url + path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert httpx.request(method, url + path).status_code == 401


def test_api_round_trip(api):
    learner, url = api
    auth = {"Authorization": "Bearer secret"}
    assert httpx.get(url + "/model", headers=auth).status_code == 404
    assert httpx.put(url + "/registry", headers=auth, json=registry_doc()).json()["entities"] > 5
    bad = httpx.post(
        url + "/events", headers=auth, json={"schema_version": 1, "events": [{"ts": 1, "kind": "x"}]}
    )
    assert bad.status_code == 400
    batch = {"schema_version": 1, "backfill": True, "events": events(household(5))}
    answer = httpx.post(url + "/events", headers=auth, json=batch).json()
    assert answer["accepted"] == len(batch["events"]) and answer["suggestions"] == []
    status = httpx.get(url + "/status", headers=auth).json()
    jsonschema.validate(status, schema("learner_status.schema.json"))
    assert status["journal"]["events"] == len(batch["events"]) and status["training"]["state"] == "never"
    assert httpx.post(url + "/replay", headers=auth, json={"automation": {}, "days": 0}).status_code == 400
    learner.train(now=at(5, 0))
    model = httpx.get(url + "/model", headers=auth).json()
    jsonschema.validate(model, schema("house_model.schema.json"))
    assert httpx.post(url + "/train", headers=auth).status_code == 202
    learner._train_thread.join(60)
    assert learner.training["state"] == "ok"
