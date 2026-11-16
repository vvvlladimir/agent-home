"""Unusual states (D35): a device in a state it is almost never in at this time of day, with (or
without) anybody home — the living-room lamp on at 03:00 while nobody is home. No model, no rule per
device: the daily scan counts how usual each state of each device the owner switches by hand is per
context cell (`predict.usual_states`), and the council's tick compares the house with it.

A note goes to the owner's inbox (info, agent `house`) when a device has been in a state for at least
`MIN_FOR_S`, that state took less than `RARE_SHARE` of the time in this cell, and the cell was seen
for at least `MIN_SEEN_MIN` minutes. Once per device and stretch; nothing is switched.
"""

from __future__ import annotations

import logging

from . import clock
from .predict import Line, anybody_home, cell

log = logging.getLogger(__name__)

MIN_FOR_S = 1800.0
RARE_SHARE = 0.02
MIN_SEEN_MIN = 600  # ten hours of this cell in the scan: a few days of it
KEY = "usual_states"


def _ts(value) -> float | None:
    from .insights import _ts as parse

    return parse(value)


class Unusual:
    def __init__(self, store, ha, rt) -> None:
        self.store, self.ha, self.rt = store, ha, rt
        self._told: set[tuple[str, str]] = set()

    def check(self, now: float | None = None) -> list[str]:
        """The notes written this time (entity ids)."""
        # the learner's longer history while it is fresh (D38), else the add-on's own scan
        table = self.store.get("learner_usual_states") or self.store.get(KEY) or {}
        if not table:
            return []
        now = now if now is not None else clock.time()
        people = {
            e: Line([(now - 1, str(s.get("state")))])
            for e, s in self.ha.states.items()
            if e.startswith("person.")
        }
        here = cell(now, anybody_home(people, now))
        written = []
        for entity_id, by_state in table.items():
            current = self.ha.states.get(entity_id) or {}
            state, since = str(current.get("state")), _ts(current.get("last_changed"))
            counts = (by_state.get(state) or {}).get(here)
            if since is None or now - since < MIN_FOR_S or counts is None:
                continue
            share = counts[0] / counts[1] if counts[1] else 1.0
            key = (entity_id, str(current.get("last_changed")))
            if counts[1] < MIN_SEEN_MIN or share >= RARE_SHARE or key in self._told:
                continue
            self._told.add(key)
            name = (current.get("attributes") or {}).get("friendly_name") or entity_id
            who = {"1": "somebody is home", "0": "nobody is home"}.get(here.split("|")[1], "")
            text = (
                f"Unusual: {name} ({entity_id}) has been {state} for {int((now - since) // 60)} min. "
                f"At this time of day{' while ' + who if who else ''} it is {state} only "
                f"{share:.0%} of the time (last {counts[1] // 60} h of such times). Nothing was switched."
            )
            try:
                self.rt.inbox_add("house", None, "info", text[:500], entity_id=entity_id)
            except Exception:  # noqa: BLE001 - a note must never stop the tick
                log.exception("unusual-state note for %s failed", entity_id)
                continue
            written.append(entity_id)
        return written
