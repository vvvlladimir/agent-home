"""A small causal household for the D35 tests: people act on causes (dark, someone there, a choice),
with a human lag, and sensors pulse with presence. Seeded, no network."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.core.insights import Change
from app.ha.registry import Registry

BASE = datetime(2026, 11, 2, 0, 0).astimezone()  # a Monday
AREAS = {
    "light.hallway": "hall", "binary_sensor.hall_motion": "hall",
    "media_player.tv": "living", "light.lamp": "living", "binary_sensor.living_motion": "living",
    "light.kitchen": "kitchen",
}  # fmt: skip


def house(extra: dict | None = None) -> SimpleNamespace:
    areas_map = {**AREAS, **(extra or {})}
    entities = [{"entity_id": e, "area_id": a} for e, a in areas_map.items()]
    areas = [{"area_id": a} for a in {a for a in areas_map.values() if a}]
    states = {e: {"state": "off", "attributes": {}} for e in areas_map}
    states["sun.sun"] = {"state": "below_horizon", "attributes": {}}
    states["person.owner"] = {"state": "home", "attributes": {}}
    return SimpleNamespace(states=states, registry=Registry.build(areas, [], entities))


def at(day: int, hours: float) -> float:
    return (BASE + timedelta(days=day, hours=hours)).timestamp()


def household(days: int = 14, seed: int = 1, *, kitchen_clock: bool = True) -> list[Change]:
    rng = random.Random(seed)
    out: list[Change] = []

    def hand(ts, e, s):
        out.append(Change(ts, e, s, "user"))

    def phys(ts, e, s):
        out.append(Change(ts, e, s, "physical"))

    for d in range(days):
        sunset, sunrise = at(d, 16.5 + rng.uniform(-0.2, 0.2)), at(d, 7.6)
        phys(sunrise, "sun.sun", "above_horizon")
        phys(sunset, "sun.sun", "below_horizon")
        # walks through the hall: the light only when it is dark
        for _ in range(rng.randint(4, 7)):
            t = at(d, rng.uniform(7, 23.5))
            phys(t, "binary_sensor.hall_motion", "on")
            dark = t >= sunset or t < sunrise
            if dark and rng.random() < 0.9:
                hand(t + rng.uniform(5, 40), "light.hallway", "on")
                hand(t + rng.uniform(100, 160), "light.hallway", "off")
            phys(t + rng.uniform(60, 90), "binary_sensor.hall_motion", "off")
        # the evening in the living room: motion, the TV when they feel like it, the lamp off at bed
        evening, bed = at(d, rng.uniform(18, 19.5)), at(d, 22.5 + rng.uniform(0, 1.5))
        t = evening
        while t < bed:
            phys(t, "binary_sensor.living_motion", "on")
            phys(t + 90, "binary_sensor.living_motion", "off")
            t += rng.uniform(300, 1200)
        hand(evening + 60, "light.lamp", "on")
        if rng.random() < 0.55:
            on = rng.uniform(evening + 600, bed - 3600)
            hand(on, "media_player.tv", "on")
            hand(bed - rng.uniform(60, 200), "media_player.tv", "off")
        hand(bed + rng.uniform(5, 40), "light.lamp", "off")
        if kitchen_clock:  # a clock habit with nothing that causes it
            hand(at(d, 6.75 + rng.uniform(-0.15, 0.15)), "light.kitchen", "on")
            hand(at(d, 7.4 + rng.uniform(-0.15, 0.15)), "light.kitchen", "off")
    return sorted(out, key=lambda c: c.ts)
