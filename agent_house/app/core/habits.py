"""Habit dossiers (D31): which devices are worth a look, what the owner does with them by hand, and
how regular it is. Deterministic, no model. A dossier that is regular enough is the prefilter's
yes: only then is the orchestrator asked to turn it into an automation (core/review.py).

A real house has ~2000 entities and most say nothing about habits. So, in order:

1. Relevance. A *device* is a light / switch / fan / cover / media player / climate / humidifier /
   input_boolean that HA shows (not hidden, disabled, a config / diagnostic entity, device noise,
   a pairing switch or T3) and that a person changed by hand (UI, app or wall switch) on at least
   `min_days` different days of the scan. Devices that switch themselves (a median of more than
   `MAX_PER_DAY` "manual" changes per active day: no context, so HA calls it physical) and devices
   one of our automations already acts on are left out. Every exclusion is counted, so the owner
   can see why nothing was proposed.
2. Episodes. Each manual change of a relevant device: local day and time; the last change of each
   *signal* within `SIGNAL_WINDOW_S` before it (motion / occupancy / presence / door / window
   sensors of the same area; people, the sun and the house mode anywhere); whether somebody was
   home, the sun, the house mode; other devices changed by hand within `TOGETHER_S`.
3. Regularities per target state, counted over the episodes: the time of day, workdays and
   weekends apart (median and spread; an episode within `TIME_BAND_MIN` of its group's median is
   "on time"); signals that preceded it, with how often the signal was followed by it
   (precision); the context shares; companions.
4. The owner's automations. Changes made by the owner's own automations, scripts or programs
   (Node-RED …: `core/insights.py`) are no hand, but they are counted per target state: a state
   they do at least `COVERED_SHARE` of the time is already automated, and a device whose every
   regular state is is left out ("covered"); otherwise the dossier says what they already do, so
   the draft adds only what is left instead of a second automation for the same thing.
5. Gate. A device qualifies when one of its target states happened on `min_days` days and at least
   `MIN_SUPPORT` of its episodes are explained by one regularity (on time, after the same signal
   with precision ≥ `MIN_PRECISION`, or together with the same device). The strongest
   `MAX_DOSSIERS` go on.

The dossier is a compact text (counts and at most `MAX_EPISODES` sample episodes per state), not
raw history: the model is asked to write one automation that captures the habit with its
variations (several triggers, `choose`, conditions the counts support) and to say what it left out.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from .agents import NEVER_OWNED, _device_class, is_noise
from .policy import resolve_tier

ACT_DOMAINS = frozenset(
    {"light", "switch", "fan", "cover", "media_player", "climate", "humidifier", "input_boolean"}
)
SKIP_STATES = frozenset({"unavailable", "unknown", ""})
SIGNAL_CLASSES = frozenset({"motion", "occupancy", "presence", "door", "window", "opening", "garage_door"})
SIGNAL_SUFFIXES = ("_motion", "_occupancy", "_presence", "_door", "_window", "_contact")
MODE_ENTITY = "input_select.agent_house_mode"
SIGNAL_WINDOW_S = 600.0
TOGETHER_S = 180.0
TIME_BAND_MIN = 45
MAX_PER_DAY = 12
MIN_SUPPORT = 0.6
MIN_PRECISION = 0.3
MIN_SIGNAL_SHARE = 0.4  # a signal worth naming preceded at least this share of the episodes
MAX_DOSSIERS = 8
MAX_EPISODES = 14
COVERED_SHARE = 0.8  # of a target state's changes done by the owner's automations: already automated
WEEKDAYS = "Mon Tue Wed Thu Fri Sat Sun".split()


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts).astimezone()


def _day(ts: float) -> str:
    return _local(ts).date().isoformat()


def _minute(ts: float) -> int:
    moment = _local(ts)
    return moment.hour * 60 + moment.minute


def _hhmm(minute: float) -> str:
    minute = int(round(minute)) % 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


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


# --- 2. episodes ---------------------------------------------------------------------------------
def is_signal(ha, entity_id: str, area: str | None) -> bool:
    domain = _domain(entity_id)
    if domain in ("person", "sun") or entity_id == MODE_ENTITY:
        return True
    if domain != "binary_sensor" or area is None or ha.registry.area_of(entity_id) != area:
        return False
    return _device_class(ha, entity_id) in SIGNAL_CLASSES or entity_id.endswith(SIGNAL_SUFFIXES)


class _Timeline:
    """State changes per entity (only real changes), for "what was it at t" and "what changed before t"."""

    def __init__(self, changes) -> None:
        self.rows: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for change in changes:
            rows = self.rows[change.entity_id]
            if change.state in SKIP_STATES or (rows and rows[-1][1] == change.state):
                continue
            rows.append((change.ts, change.state))

    def at(self, entity_id: str, ts: float) -> str | None:
        state = None
        for t, value in self.rows.get(entity_id, ()):
            if t > ts:
                break
            state = value
        return state

    def last_before(self, entity_id: str, ts: float, window: float) -> tuple[float, str] | None:
        found = None
        for t, value in self.rows.get(entity_id, ()):
            if t > ts:
                break
            if ts - t <= window:
                found = (t, value)
        return found


def episodes(entity_id: str, changes, ha, timeline: _Timeline) -> list[dict]:
    area = ha.registry.area_of(entity_id)
    signals = [e for e in timeline.rows if e != entity_id and is_signal(ha, e, area)]
    people = [e for e in timeline.rows if _domain(e) == "person"]
    hand = [
        c for c in changes if c.manual and _domain(c.entity_id) in ACT_DOMAINS and c.state not in SKIP_STATES
    ]
    out = []
    for change in (c for c in hand if c.entity_id == entity_id):
        before = []  # a person arriving or leaving is a signal too; being home is context below
        for signal in signals:
            hit = timeline.last_before(signal, change.ts, SIGNAL_WINDOW_S)
            if hit:
                before.append((signal, hit[1], int(change.ts - hit[0])))
        states = [timeline.at(p, change.ts) for p in people]
        known = [s for s in states if s is not None]
        moment = _local(change.ts)
        out.append({
            "ts": change.ts, "state": change.state, "day": moment.date().isoformat(),
            "weekday": moment.weekday(), "minute": moment.hour * 60 + moment.minute,
            "before": sorted(before, key=lambda b: b[2]),
            "home": (any(s == "home" for s in known) if known else None),
            "sun": timeline.at("sun.sun", change.ts),
            "mode": timeline.at(MODE_ENTITY, change.ts),
            "together": sorted({
                f"{c.entity_id} → {c.state}" for c in hand
                if c.entity_id != entity_id and abs(c.ts - change.ts) <= TOGETHER_S
            }),
        })  # fmt: skip
    return out


def automated(entity_id: str, changes) -> dict[str, list[dict]]:
    """target state → changes the owner's automations, scripts or programs made to it, as
    {ts, day, weekday, minute, ref} (ours never get here: their devices are skipped)."""
    out: dict[str, list[dict]] = defaultdict(list)
    for c in changes:
        if c.entity_id == entity_id and c.cause in ("automation", "script") and c.state not in SKIP_STATES:
            moment = _local(c.ts)
            out[c.state].append({
                "ts": c.ts, "day": moment.date().isoformat(), "weekday": moment.weekday(),
                "minute": moment.hour * 60 + moment.minute, "ref": c.ref or c.cause,
            })  # fmt: skip
    return out


def _by_owner(state: str, manual: int, done: list[dict], today: date) -> dict:
    refs = Counter(d["ref"] for d in done)
    times = [{k: v for k, v in t.items() if k != "on_time_ids"} for t in _time_groups(done, today)]
    return {
        "state": state, "count": len(done), "by": [r for r, _ in refs.most_common(3)],
        "share": round(len(done) / (len(done) + manual), 2), "time": times,
    }  # fmt: skip


# --- 3. regularities -----------------------------------------------------------------------------
def _time_groups(eps: list[dict], today: date) -> list[dict]:
    workdays = [e for e in eps if e["weekday"] < 5]
    weekend = [e for e in eps if e["weekday"] >= 5]
    if not workdays or not weekend:
        # one-sided (stories run 2026-11-14, bug 3: a Mon–Fri habit drafted for every day). It is a
        # weekday habit only when a day of the other kind passed without it since the first episode
        # (the scan's end counts, not the last episode: a Mon–Fri habit never has a Saturday between
        # its own episodes in its first week). Until then nobody can tell, so "unknown": the dossier
        # waits instead of the model guessing days from the samples (stories run 2026-11-14 #2:
        # three episodes Mon–Wed became `weekday: [mon, tue, wed]`).
        first = min(date.fromisoformat(e["day"]) for e in eps)
        passed = [first + timedelta(n) for n in range(max((today - first).days, 0))]
        other = any(d.weekday() >= 5 for d in passed) if workdays else any(d.weekday() < 5 for d in passed)
        groups = (("Mon–Fri" if workdays else "Sat–Sun", eps),) if other else (("unknown", eps),)
    elif _close(workdays, weekend):
        groups = (("every day", eps),)
    else:
        groups = (("Mon–Fri", workdays), ("Sat–Sun", weekend))
    out = []
    for label, group in groups:
        if not group:
            continue
        center = _circular_median([e["minute"] for e in group])
        offsets = [_offset(e["minute"], center) for e in group]
        on_time = [e for e, o in zip(group, offsets, strict=True) if abs(o) <= TIME_BAND_MIN]
        out.append({
            "days": label, "at": _hhmm(center), "spread_min": int(statistics.median(abs(o) for o in offsets)),
            "on_time": len(on_time), "of": len(group), "on_time_ids": {id(e) for e in on_time},
        })  # fmt: skip
    return out


def _close(a: list[dict], b: list[dict]) -> bool:
    center_a = _circular_median([e["minute"] for e in a])
    center_b = _circular_median([e["minute"] for e in b])
    return abs(_offset(center_a, center_b)) <= TIME_BAND_MIN


def _offset(minute: float, center: float) -> float:
    return (minute - center + 720) % 1440 - 720


def _circular_median(minutes: list[int]) -> float:
    """Median time of day that does not break around midnight: anchored at the first sample."""
    anchor = minutes[0]
    return (anchor + statistics.median(_offset(m, anchor) for m in minutes)) % 1440


def regularities(entity_id: str, state: str, eps: list[dict], timeline: _Timeline, today: date) -> dict:
    n = len(eps)
    times = _time_groups(eps, today)
    explained_by_time = set().union(*(t.pop("on_time_ids") for t in times)) if times else set()
    signal_hits: dict[tuple[str, str], list[int]] = defaultdict(list)
    for e in eps:
        for signal, value, delay in e["before"]:
            signal_hits[(signal, value)].append(delay)
    signals = []
    for (signal, value), delays in signal_hits.items():
        if len(delays) < max(2, MIN_SIGNAL_SHARE * n):
            continue
        fired = [t for t, v in timeline.rows.get(signal, ()) if v == value]
        followed = sum(1 for t in fired if any(0 <= e["ts"] - t <= SIGNAL_WINDOW_S for e in eps))
        signals.append({
            "signal": signal, "state": value, "before": len(delays), "of": n,
            "median_delay_s": int(statistics.median(delays)), "fired": len(fired),
            "precision": round(followed / len(fired), 2) if fired else 0.0,
        })  # fmt: skip
    signals.sort(key=lambda s: (-s["before"], -s["precision"]))
    together = Counter(t for e in eps for t in e["together"])
    companions = [
        {"with": k, "times": v, "of": n} for k, v in together.most_common(3) if v >= max(2, 0.5 * n)
    ]
    home = [e["home"] for e in eps if e["home"] is not None]
    sun = [e["sun"] for e in eps if e["sun"]]
    modes = Counter(e["mode"] for e in eps if e["mode"])
    support = max(
        [len(explained_by_time) / n]
        + [s["before"] / n for s in signals if s["precision"] >= MIN_PRECISION]
        + [c["times"] / n for c in companions]
    )
    return {
        "state": state, "count": n, "days": len({e["day"] for e in eps}),
        "time": times, "after": signals[:3], "together": companions,
        "home": [sum(home), len(home)] if home else None,
        "sun_below": [sum(1 for s in sun if s == "below_horizon"), len(sun)] if sun else None,
        "modes": dict(modes.most_common(3)),
        "support": round(support, 2),
        "days_known": all(t["days"] != "unknown" for t in times),
    }  # fmt: skip


# --- 4. dossiers ---------------------------------------------------------------------------------
def build(
    changes,
    ha,
    *,
    days: int,
    min_days: int,
    policy: dict | None = None,
    skip=frozenset(),
    today: date | None = None,
) -> tuple[list, dict]:
    """(dossiers that pass the gate, strongest first; the funnel incl. how many passed). `today`:
    the scan's end (default: the day after the last change); days before it are complete."""
    changes = sorted(changes, key=lambda c: c.ts)
    if today is None:
        today = _local(changes[-1].ts).date() + timedelta(1) if changes else date.today()
    candidates, funnel = relevant(changes, ha, min_days=min_days, policy=policy, skip=skip)
    timeline = _Timeline(changes)
    out, weak, waiting, covered = [], [], [], []
    for candidate in candidates:
        entity_id = candidate["entity_id"]
        eps = episodes(entity_id, changes, ha, timeline)
        by_state: dict[str, list[dict]] = defaultdict(list)
        for e in eps:
            by_state[e["state"]].append(e)
        theirs = automated(entity_id, changes)
        by_owner = {s: _by_owner(s, len(by_state.get(s, [])), done, today) for s, done in theirs.items()}
        states = [regularities(entity_id, s, group, timeline, today) for s, group in by_state.items()]
        qualified = [s for s in states if s["days"] >= min_days and s["support"] >= MIN_SUPPORT]
        if not qualified:
            weak.append(entity_id)
            continue
        left = [s for s in qualified if by_owner.get(s["state"], {}).get("share", 0) < COVERED_SHARE]
        if not left:
            refs = sorted({r for s in qualified for r in by_owner[s["state"]]["by"]})
            covered.append({"entity_id": entity_id, "by": refs})
            continue
        if not all(s["days_known"] for s in left):
            waiting.append(entity_id)
            continue
        strength = max(s["support"] * s["days"] for s in qualified)
        out.append({
            "entity_id": entity_id, "name": ha.registry.name_of(entity_id)
            or (ha.states.get(entity_id) or {}).get("attributes", {}).get("friendly_name"),
            "room": ha.registry.area_of(entity_id), "days_scanned": days, "days": candidate["days"],
            "count": candidate["count"], "states": sorted(states, key=lambda s: -s["count"]),
            "strength": round(strength, 2), "mode_helper": MODE_ENTITY in ha.states,
            "samples": {s: _samples(group) for s, group in by_state.items()},
            "by_owner": sorted(by_owner.values(), key=lambda b: -b["count"]),
        })  # fmt: skip
    out.sort(key=lambda d: -d["strength"])
    funnel["irregular"] = len(weak)
    funnel["waiting_for_weekdays"] = waiting
    funnel["covered_by_owner"] = covered  # the owner's automations already do what the hand did
    if covered:
        funnel["left_out"]["the owner's own automation already does it"] = len(covered)
    funnel["dossiers"] = min(len(out), MAX_DOSSIERS)
    return out[:MAX_DOSSIERS], funnel


def _samples(eps: list[dict]) -> list[str]:
    """At most `MAX_EPISODES` episodes as short lines, spread over the scan (not just the latest)."""
    step = max(1, len(eps) / MAX_EPISODES)
    picked = [eps[int(i * step)] for i in range(min(len(eps), MAX_EPISODES))]
    lines = []
    for e in picked:
        bits = [f"{WEEKDAYS[e['weekday']]} {e['day'][5:]} {_hhmm(e['minute'])}"]
        bits += [f"{s} → {v} {_ago(d)} before" for s, v, d in e["before"][:3]]
        if e["home"] is not None:
            bits.append("someone home" if e["home"] else "nobody home")
        if e["sun"]:
            bits.append("sun " + e["sun"].replace("_", " "))
        if e["together"]:
            bits.append("with " + ", ".join(e["together"][:2]))
        lines.append(" · ".join(bits))
    return lines


def _ago(seconds: int) -> str:
    return f"{seconds} s" if seconds < 90 else f"{round(seconds / 60)} min"


def summary(dossier: dict) -> str:
    """One line per qualified target state, for titles and the proposal's Evidence."""
    parts = []
    for s in dossier["states"]:
        bits = [
            f"{t['days']} about {t['at']} ({t['on_time']}/{t['of']})" for t in s["time"] if t["on_time"] >= 2
        ]
        bits += [f"after {a['signal']} → {a['state']} ({a['before']}/{a['of']})" for a in s["after"][:1]
                 if a["precision"] >= MIN_PRECISION]  # fmt: skip
        parts.append(
            f"→ {s['state']} {s['count']}× on {s['days']} days" + (f": {'; '.join(bits)}" if bits else "")
        )
    for b in dossier.get("by_owner") or []:
        parts.append(f"already automated → {b['state']} {b['count']}× by {', '.join(b['by'])}")
    return " | ".join(parts)


def request(dossier: dict) -> str:
    """What the orchestrator is asked: the dossier as text, and how to treat a habit."""
    name = f' "{dossier["name"]}"' if dossier.get("name") else ""
    lines = [
        f"Device: {dossier['entity_id']}{name}, area {dossier['room'] or 'none'}.",
        f"Changed by hand {dossier['count']} times on {dossier['days']} of the last "
        f"{dossier['days_scanned']} days. Counted from Home Assistant's logbook, no model involved:",
    ]
    for s in dossier["states"]:
        lines.append(f"→ {s['state']}: {s['count']} times on {s['days']} days")
        for t in s["time"]:
            lines.append(f"  time: {t['days']} median {t['at']}, typical spread ±{t['spread_min']} min, "
                         f"{t['on_time']} of {t['of']} within ±{TIME_BAND_MIN} min")  # fmt: skip
        for a in s["after"]:
            lines.append(
                f"  after {a['signal']} → {a['state']}: {a['before']} of {a['of']} times, median "
                f"{_ago(a['median_delay_s'])} before; that signal did so {a['fired']} times, followed "
                f"{int(a['precision'] * 100)}% of the time"
            )
        if s["home"]:
            lines.append(f"  someone home: {s['home'][0]} of {s['home'][1]}")
        if s["sun_below"]:
            lines.append(f"  sun below the horizon: {s['sun_below'][0]} of {s['sun_below'][1]}")
        if s["modes"]:
            lines.append("  house mode: " + ", ".join(f"{k} {v}" for k, v in s["modes"].items()))
        for c in s["together"]:
            lines.append(f"  together with {c['with']}: {c['times']} of {c['of']}")
        lines.append("  samples: " + " | ".join(dossier["samples"].get(s["state"], [])))
    for b in dossier.get("by_owner") or []:
        when = "; ".join(f"{t['days']} median {t['at']}" for t in b["time"])
        lines.append(
            f"Already automated → {b['state']}: {b['count']} times by the owner's {', '.join(b['by'])} "
            f"({when}; not counted above, {int(b['share'] * 100)}% of all changes to {b['state']})"
        )
    # the house mode only when the house has the helper: a condition on a missing entity is false
    # (stories run 2026-11-14, bug 1); dossiers saved before the flag existed count as without it
    conditions = (
        "someone being home, the sun or the house mode input_select.agent_house_mode"
        if dossier.get("mode_helper")
        else "someone being home or the sun"
    )
    lines.append(
        "Write ONE automation that does for the owner what they do here by hand, the way they do it. "
        "Habits are not exact: keep the variations the counts show (for example a different time on "
        "weekends, several triggers with trigger ids and a `choose` for on and off, a condition on "
        f"{conditions} when most episodes share it). A time line for Mon–Fri or Sat–Sun alone means "
        "the habit is not seen on the other days: add `condition: time` with those `weekday:` values "
        "(mon, tue, wed, thu, fri or sat, sun). Take weekdays only from those time lines, never from "
        "the weekdays of the samples; an `every day` line gets no weekday condition. Use a sensor "
        "trigger only when its share and "
        "precision are high; else the time. Leave out what happened only once or twice. In the "
        "rationale, name the counts you relied on and what you left out."
    )
    if dossier.get("by_owner"):
        lines.append(
            "The owner's own automations already do part of this (the 'Already automated' lines). "
            "Do not repeat it: cover only what they leave to the owner's hand, with triggers or "
            "conditions that keep both from acting at the same time. You cannot change the owner's "
            "automations; if the hand only fills a gap in one of them, say so in the rationale."
        )
    return "\n".join(lines)
