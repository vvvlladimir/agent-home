"""Shadow evidence for an automation draft: "what would this have done in the last N days".

The draft's triggers are replayed against Home Assistant's recorded history. Simulated:
- `state` (entity_id, from, to; `for` is ignored),
- `numeric_state` (entity_id, above, below: entering the range counts),
- `time` with fixed `at` times (once per day).
Everything else (sun, template, event, time_pattern, attributes, value_template, …) is listed in
`unsupported`; then `supported` is false and `would_fire` is a lower bound.

The evidence then checks the conditions at each firing, from the same history: `state`,
`numeric_state`, `time` (after / before / weekday) and `and` / `or` / `not` of those. `blocked` is
how many firings they stopped. A condition it cannot check (template, sun, zone, attribute, `for`,
an entity as a time, …) lets the firing through, is listed in `unsupported` and makes
`conditions_evaluated` and `supported` false: `would_fire` is then an upper bound for that part.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

MAX_SAMPLES = 20
SKIP_STATES = frozenset({"unavailable", "unknown"})


def _list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _ts(text: str) -> float | None:
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).timestamp()


def _number(state: object) -> float | None:
    try:
        return float(state)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _in_range(value: float | None, above, below) -> bool:
    if value is None:
        return False
    if isinstance(above, int | float) and not value > above:
        return False
    return not (isinstance(below, int | float) and not value < below)


def _transitions(rows: list[dict]) -> list[tuple[float, str | None, str]]:
    """(ts, old, new) for each change; the first row is the state at the window's start."""
    out, previous = [], None
    for index, row in enumerate(rows):
        state = str(row.get("state"))
        ts = _ts(row.get("last_changed") or row.get("last_updated") or "")
        if index > 0 and state != previous and ts is not None:
            out.append((ts, previous, state))
        previous = state
    return out


async def firings(
    config: dict, ha, days: int = 7, now: datetime | None = None
) -> tuple[list[dict], list[str]]:
    """([{ts, trigger}] newest first, unsupported notes) for every trigger firing in the window."""
    days = max(1, min(int(days), 30))
    end = now or datetime.now(UTC)
    start = end - timedelta(days=days)
    unsupported: list[str] = []
    samples: list[dict] = []
    cache: dict[str, list[dict]] = {}

    async def history(entity_id: str) -> list[dict]:
        if entity_id not in cache:
            cache[entity_id] = await ha.history(entity_id, start.isoformat(), end.isoformat())
        return cache[entity_id]

    def hit(ts: float, label: str) -> None:
        samples.append({"ts": ts, "trigger": label})

    for index, trigger in enumerate(_list(config.get("triggers"))):
        if not isinstance(trigger, dict):
            continue
        kind = trigger.get("trigger", trigger.get("platform"))
        label = f"#{index + 1} {kind}"
        if kind in ("state", "numeric_state") and (trigger.get("attribute") or trigger.get("value_template")):
            unsupported.append(f"{label}: attribute / value_template")
            continue
        if kind == "state":
            to, frm = _list(trigger.get("to")), _list(trigger.get("from"))
            for entity_id in _list(trigger.get("entity_id")):
                for ts, old, new in _transitions(await history(str(entity_id))):
                    if (to and new not in map(str, to)) or (frm and old not in map(str, frm)):
                        continue
                    if not to and new in SKIP_STATES:
                        continue
                    hit(ts, f"{entity_id}: {old} → {new}")
            if trigger.get("for"):
                unsupported.append(f"{label}: `for` is ignored (counts are an upper bound)")
        elif kind == "numeric_state":
            above, below = trigger.get("above"), trigger.get("below")
            if not isinstance(above, int | float) and not isinstance(below, int | float):
                unsupported.append(f"{label}: above/below from an entity")
                continue
            for entity_id in _list(trigger.get("entity_id")):
                for ts, old, new in _transitions(await history(str(entity_id))):
                    if _in_range(_number(new), above, below) and not _in_range(_number(old), above, below):
                        hit(ts, f"{entity_id}: {old} → {new}")
        elif kind == "time":
            for at in _list(trigger.get("at")):
                try:
                    parts = [int(p) for p in str(at).split(":")]
                    hour, minute, second = (parts + [0, 0])[:3]
                except ValueError:
                    unsupported.append(f"{label}: at {at} (an entity)")
                    continue
                local = end.astimezone()
                for back in range(days + 1):
                    moment = (local - timedelta(days=back)).replace(
                        hour=hour, minute=minute, second=second, microsecond=0
                    )
                    if start <= moment <= end:
                        hit(moment.timestamp(), f"time {hour:02d}:{minute:02d}")
        else:
            unsupported.append(f"{label}: cannot be simulated from history")

    samples.sort(key=lambda s: s["ts"], reverse=True)
    return samples, unsupported


def _state_at(rows: list[dict], ts: float) -> str | None:
    """The entity's state at `ts` (the last recorded change at or before it)."""
    state = None
    for row in rows:
        at = _ts(row.get("last_changed") or row.get("last_updated") or "")
        if at is not None and at > ts and state is not None:
            break
        state = str(row.get("state"))
    return state


def _clock(text) -> tuple[int, int, int] | None:
    try:
        parts = [int(p) for p in str(text).split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3:
        return None
    hour, minute, second = (parts + [0, 0])[:3]
    return hour, minute, second


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class _Conditions:
    """Evaluates HA conditions at a moment from recorded history: True / False, or None when it
    cannot tell (noted once in `notes`)."""

    def __init__(self, history) -> None:
        self.history, self.notes = history, []

    def _cannot(self, label: str, why: str) -> None:
        note = f"condition {label}: {why} (not evaluated)"
        if note not in self.notes:
            self.notes.append(note)

    async def every(self, conditions, ts: float, prefix: str = "") -> bool | None:
        results = [await self.one(c, ts, f"{prefix}#{i + 1}") for i, c in enumerate(_list(conditions))]
        if False in results:
            return False
        return None if None in results else True

    async def one(self, cond, ts: float, label: str) -> bool | None:
        if not isinstance(cond, dict):
            self._cannot(label, "a template shorthand")
            return None
        kind = cond.get("condition")
        label = f"{label} {kind}"
        if kind == "and":
            return await self.every(cond.get("conditions"), ts, f"{label}.")
        if kind == "or":
            results = [
                await self.one(c, ts, f"{label}.#{i + 1}")
                for i, c in enumerate(_list(cond.get("conditions")))
            ]
            if True in results:
                return True
            return None if None in results else False
        if kind == "not":
            results = [
                await self.one(c, ts, f"{label}.#{i + 1}")
                for i, c in enumerate(_list(cond.get("conditions")))
            ]
            if True in results:
                return False
            return None if None in results else True
        if kind in ("state", "numeric_state") and (cond.get("attribute") or cond.get("value_template")):
            self._cannot(label, "attribute / value_template")
            return None
        if kind == "state":
            if cond.get("for"):
                self._cannot(label, "`for`")
                return None
            wanted = [str(v) for v in _list(cond.get("state"))]
            if any("." in w for w in wanted):  # an entity as the state: its value at ts is unknown here
                self._cannot(label, "a state taken from an entity")
                return None
            hits = [_state_at(await self.history(str(e)), ts) in wanted for e in _list(cond.get("entity_id"))]
            if not hits:
                return None
            return any(hits) if cond.get("match") == "any" else all(hits)
        if kind == "numeric_state":
            above, below = cond.get("above"), cond.get("below")
            if not isinstance(above, int | float | None) or not isinstance(below, int | float | None):
                self._cannot(label, "above/below from an entity")
                return None
            hits = [
                _in_range(_number(_state_at(await self.history(str(e)), ts)), above, below)
                for e in _list(cond.get("entity_id"))
            ]
            return all(hits) if hits else None
        if kind == "time":
            moment = datetime.fromtimestamp(ts).astimezone()
            days = [str(d) for d in _list(cond.get("weekday"))]
            if days and WEEKDAYS[moment.weekday()] not in days:
                return False
            after, before = cond.get("after"), cond.get("before")
            start = _clock(after) if after is not None else (0, 0, 0)
            end = _clock(before) if before is not None else None
            if start is None or (before is not None and end is None):
                self._cannot(label, "a time taken from an entity")
                return None
            now = (moment.hour, moment.minute, moment.second)
            if end is None:
                return now >= start
            return start <= now < end if start <= end else (now >= start or now < end)  # past midnight
        self._cannot(label, "cannot be checked from history")
        return None


def fully_supported(unsupported: list[str]) -> bool:
    return not [u for u in unsupported if "ignored" not in u]


async def simulate(config: dict, ha, days: int = 7, now: datetime | None = None) -> dict:
    days = max(1, min(int(days), 30))
    end = now or datetime.now(UTC)
    samples, unsupported = await firings(config, ha, days, end)
    cache: dict[str, list[dict]] = {}
    start = end - timedelta(days=days)

    async def history(entity_id: str) -> list[dict]:
        if entity_id not in cache:
            cache[entity_id] = await ha.history(entity_id, start.isoformat(), end.isoformat())
        return cache[entity_id]

    checker = _Conditions(history)
    passed = [s for s in samples if await checker.every(config.get("conditions"), s["ts"]) is not False]
    return {
        "days": days,
        "supported": fully_supported(unsupported) and not checker.notes,
        "unsupported": unsupported + checker.notes,
        "fired": len(samples),
        "blocked": len(samples) - len(passed),
        "would_fire": len(passed),
        "samples": passed[:MAX_SAMPLES],
        "conditions_evaluated": not checker.notes,
        "computed": time.time(),
    }
