"""Lived days in house time (sim/days.py) and restoring a snapshot (sim/snapshot.py), fakes only."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from sim import clock, polygon
from sim.run import Ports, boot
from sim.sim_config import SimConfig


@pytest.fixture(autouse=True)
def _back_to_real_time():
    yield
    clock.reset()  # later tests in this process expect the system clock


def test_two_days_pass_the_checks_and_a_snapshot_boots_back(tmp_path):
    out = tmp_path / "out"
    rc = polygon.main(["days", "--days", "2", "--seed", "2", "--out-dir", str(out),
                       "--sim-config", str(tmp_path / "no-sim-config")])  # fmt: skip
    report = json.loads((out / "report.json").read_text())
    s = report["scenarios"][0]
    assert rc == 0 and report["ok"], [c for c in s["checks"] if not c["ok"]]
    assert s["house_days"] == 2 and not s["stuck"] and s["runs"]
    assert {r["agent_id"] for r in s["runs"]} >= {"orchestrator", "hallway"}
    assert len(s["snapshots"]) == 2, "one per house midnight"

    snap = Path(s["snapshots"][-1])
    saved = json.loads((snap / "ha.json").read_text())["states"]
    stack = boot(Ports.pick(0), tmp_path / "restored", SimConfig(), rate=0.0, restore=snap)
    try:
        app = httpx.Client(base_url=stack.app_url, timeout=30)
        assert stack.on_ha(lambda: {e: v["state"] for e, v in stack.ha.states.items()}) == {
            e: v["state"] for e, v in saved.items()
        }
        assert clock.time() >= json.loads((snap / "snapshot.json").read_text())["house_time"]
        runs_then = [
            r
            for r in s["runs"]
            if r["started"] < json.loads((snap / "snapshot.json").read_text())["house_time"]
        ]
        assert len(app.get("/api/runs", params={"limit": 200}).json()) == min(len(runs_then), 200)
    finally:
        stack.stop()
