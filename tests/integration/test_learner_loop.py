"""D38 end to end, in one process: the real add-on on the fake Home Assistant, the real learner server
(jetson/learner/server.py) on an ephemeral port. The history before the add-on started comes from the
logbook, what happens after it live with its cause; the learner trains when asked."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from .test_m1_skeleton import wait_for
from .test_m4_automations import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "jetson"))

from learner.server import Learner  # noqa: E402
from learner.server import serve as serve_learner  # noqa: E402


@pytest.fixture
def learner(tmp_path):
    learner = Learner(tmp_path / "learner", follow_time_zone=False)  # one process: keep its TZ
    server = serve_learner(learner, "127.0.0.1", 0, "jetson-sim")  # the token the add-on is given
    threading.Thread(target=server.serve_forever, daemon=True).start()
    learner.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield learner
    server.shutdown()
    learner.journal.close()


def journal(learner) -> list[tuple]:
    rows = learner.journal.db.execute("SELECT kind, entity_id, state, cause, ref FROM events ORDER BY ts")
    return rows.fetchall()


def test_the_house_reaches_the_learner_with_its_causes(settings, ha_state, learner):
    hour_ago = time.time() - 3600
    ha_state.user_change("light.hallway", "on", ts=hour_ago)  # before the add-on: the logbook has it
    ha_state.physical_change("light.hallway", "off", ts=hour_ago + 60)
    app = serve(settings, jetson_learner_url=learner.url)
    try:
        wait_for(lambda: learner.registry is not None, timeout=15)
        wait_for(lambda: ("state", "light.hallway", "on", "user", None) in journal(learner), timeout=15)
        ha_state.run_automation("automation.hall_light", [("light.hallway", "on")])
        ha_state.user_change("light.hallway", "off")
        rows = wait_for(lambda: len(journal(learner)) >= 5 and journal(learner), timeout=15)
        assert ("trigger", "automation.hall_light", None, "automation", "automation.hall_light") in rows
        assert ("state", "light.hallway", "on", "automation", "automation.hall_light") in rows  # live, by context
        assert ("state", "light.hallway", "off", "user", None) in rows
        assert ("state", "light.hallway", "off", "physical", None) in rows  # from the logbook
        info = httpx.get(app.base + "/api/learner").json()
        assert info["configured"] and info["mode"] == "shadow" and info["link"]["connected"]
        assert info["link"]["pushed"] >= 2 and info["link"]["backfill"]["state"] == "done"
        assert info["jetson"]["journal"]["events"] >= 2  # refreshed when the backfill was done
        assert httpx.post(app.base + "/api/learner/train").status_code == 202
        wait_for(lambda: learner.training["state"] in ("ok", "failed"), timeout=60)
        assert learner.training["state"] == "ok", learner.training
    finally:
        app.close()


def test_no_learner_configured_means_no_link(app_server):
    info = httpx.get(app_server.url + "/api/learner").json()
    assert not info["configured"] and info["model"] is None
    assert httpx.post(app_server.url + "/api/learner/train").status_code == 409
