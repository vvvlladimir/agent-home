"""House time (app/core/clock.py) and the simulated household (sim/household.py)."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from app.core import clock
from sim.household import Chaos, plan, summary


@pytest.fixture(autouse=True)
def _real_time():
    clock.reset()
    yield
    clock.reset()


def test_production_house_time_is_the_system_clock():
    assert clock.offset() == 0
    assert abs(clock.time() - time.time()) < 0.5
    assert abs(clock.monotonic() - time.monotonic()) < 0.5
    assert abs(clock.local().timestamp() - time.time()) < 0.5 and clock.local().tzinfo is not None


def test_house_time_only_moves_forward():
    t = clock.advance(3600)
    assert t - time.time() == pytest.approx(3600, abs=0.5)
    assert clock.monotonic() - time.monotonic() == pytest.approx(3600, abs=0.5)
    with pytest.raises(ValueError):
        clock.advance(-1)
    before = clock.time()
    assert clock.travel_to(before - 86400) == pytest.approx(before, abs=0.5), "the past is a no-op"
    assert clock.travel_to(before + 86400) == pytest.approx(before + 86400, abs=0.5)
    assert clock.now(UTC).tzinfo is UTC


START = datetime(2026, 10, 5, tzinfo=UTC)  # a Monday


def test_a_household_plan_is_reproducible_and_sorted():
    a, b = plan(14, START, seed=3), plan(14, START, seed=3)
    assert a == b and a != plan(14, START, seed=4)
    assert [e.ts for e in a] == sorted(e.ts for e in a)
    assert all(START.timestamp() <= e.ts < START.timestamp() + 15 * 86400 for e in a)


def test_people_are_not_machines_and_sensors_lie_rarely():
    events = plan(30, START, seed=7)
    tags = summary(events)["tags"]
    faults = sum(n for t, n in tags.items() if t.startswith("fault:"))
    assert tags["human"] > 50, "sick days, guests, a trip, late nights, forgotten lights"
    assert 0 < faults < 0.05 * len(events), "lies are rare"
    beds = sorted({datetime.fromtimestamp(e.ts, UTC).strftime("%H:%M") for e in events
                   if e.entity_id == "light.bedroom_lamp" and e.state == "on"})  # fmt: skip
    assert len(beds) > 20, "nobody goes to bed at the same minute twice"


def test_a_calm_household_has_no_lies():
    tags = summary(plan(14, START, seed=7, chaos=Chaos.calm()))["tags"]
    assert not [t for t in tags if t.startswith("fault:")] and "human" not in tags
