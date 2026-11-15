"""Which devices are worth a habit prediction (D35, core/predict.py): relevance only.

A real house has ~2000 entities and most say nothing about habits. A *device* is a light / switch /
fan / cover / media player / climate / humidifier / input_boolean / input_select (the house
mode too) that HA shows (not hidden,
disabled, a config / diagnostic entity, device noise, a pairing switch or T3) and that a person
changed by hand (UI, app or wall switch) on at least `min_days` different days of the scan. Devices
that switch themselves (a median of more than `MAX_PER_DAY` "manual" changes per active day: no
context, so HA calls it physical) and devices one of our automations already acts on are left out.
Every exclusion is counted, so the owner can see why nothing was proposed.

These are safety and noise rules, not taste: whether a device's state is worth automating is
decided by the prediction and its cost, never by its domain."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime

from .agents import NEVER_OWNED, is_noise
from .policy import resolve_tier

ACT_DOMAINS = frozenset(
    {
        "light",
        "switch",
        "fan",
        "cover",
        "media_player",
        "climate",
        "humidifier",
        "input_boolean",
        "input_select",
    }
)
SKIP_STATES = frozenset({"unavailable", "unknown", ""})
MAX_PER_DAY = 12


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts).astimezone()


def _day(ts: float) -> str:
    return _local(ts).date().isoformat()


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


# --- 1. relevance ------------------------------------------------------------------------------
def exclusion(ha, entity_id: str, policy: dict | None) -> str | None:
    """Why a device is no habit candidate whatever its history, or None."""
    if _domain(entity_id) not in ACT_DOMAINS:
        return "not a switchable device"
    if entity_id not in ha.states:
        return "unknown to Home Assistant now"
    meta = ha.registry.entity_meta.get(entity_id, {})
    if ha.registry.is_hidden(entity_id):
        return "hidden or disabled"
    if meta.get("entity_category"):
        return "a device setting or diagnostic"
    if NEVER_OWNED.search(entity_id):
        return "a pairing switch"
    if is_noise(ha, entity_id):
        return "device noise"
    if policy is not None and resolve_tier(entity_id, policy) == "T3":
        return "T3: never automated"
    return None


def relevant(
    changes, ha, *, min_days: int, policy: dict | None = None, skip=frozenset()
) -> tuple[list, dict]:
    """(devices worth a dossier, strongest first, as {entity_id, days, count}; the funnel: how many
    entities each step left, and the reasons the others were left out)."""
    manual: dict[str, list] = defaultdict(list)
    for change in changes:
        if change.manual and change.state not in SKIP_STATES and _domain(change.entity_id) in ACT_DOMAINS:
            manual[change.entity_id].append(change)
    reasons: Counter = Counter()
    near: list[dict] = []
    out = []
    for entity_id, events in manual.items():
        why = exclusion(ha, entity_id, policy)
        if why is None and entity_id in skip:
            why = "one of our automations already acts on it"
        if why is None:
            per_day = Counter(_day(e.ts) for e in events)
            if len(per_day) < min_days:
                why = f"changed by hand on fewer than {min_days} days"
                near.append({"entity_id": entity_id, "days": len(per_day), "needed": min_days})
            elif statistics.median(per_day.values()) > MAX_PER_DAY:
                why = "changes too often to be a person (it switches itself)"
        if why:
            reasons[why] += 1
            continue
        out.append({"entity_id": entity_id, "days": len({_day(e.ts) for e in events}), "count": len(events)})
    out.sort(key=lambda c: (-c["days"], -c["count"], c["entity_id"]))
    switchable = sum(1 for e in ha.states if _domain(e) in ACT_DOMAINS)
    funnel = {
        "entities": len(ha.states),
        "switchable": switchable,
        "changed_by_hand": len(manual),
        "relevant": len(out),
        "left_out": dict(reasons.most_common()),
        "near": sorted(near, key=lambda n: -n["days"])[:10],
    }
    return out, funnel
