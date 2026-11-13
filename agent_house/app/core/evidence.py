"""Shadow evidence for an automation draft: "what would this have done in the last N days".

The draft's triggers are replayed against Home Assistant's recorded history. Simulated:
- `state` (entity_id, from, to; `for` is ignored),
- `numeric_state` (entity_id, above, below: entering the range counts),
- `time` with fixed `at` times (once per day).
Everything else (sun, template, event, time_pattern, attributes, value_template, …) is listed in
`unsupported`; then `supported` is false and `would_fire` is a lower bound. Conditions are not
evaluated either: `would_fire` counts trigger firings, which the GUI says.
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


def fully_supported(unsupported: list[str]) -> bool:
    return not [u for u in unsupported if "ignored" not in u]


async def simulate(config: dict, ha, days: int = 7, now: datetime | None = None) -> dict:
    days = max(1, min(int(days), 30))
    samples, unsupported = await firings(config, ha, days, now)
    return {
        "days": days,
        "supported": fully_supported(unsupported),
        "unsupported": unsupported,
        "would_fire": len(samples),
        "samples": samples[:MAX_SAMPLES],
        "conditions_evaluated": False,
        "computed": time.time(),
    }
