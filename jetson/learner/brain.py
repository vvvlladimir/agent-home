"""What the learner learns from the journal (D38). Rebuilt every night; counts and one model kind.

Layers, each answering one question with counts the owner could check by hand:

- habits: the D35 predictions (core/predict.py `scan`, the very same code as the add-on) over a longer
  history than HA keeps. Their automations are built deterministically and stay HA automations, so the
  house keeps them when the Jetson is off.
- live: per device and state a gradient-boosted model (scikit-learn's histogram GBDT, the family of
  LightGBM, measured in sim/proto_learner.py) on the same samples and features as the D35 tree. Its
  out-of-sample record is kept per probability bin exactly like predict.py keeps it per rule (k-fold by
  day, fire once per stretch, back off after a wrong one), so a live suggestion carries the same kind
  of earned precision. Prototype: +41…+57 % utility over the tree with PIR sensors, +0…+47 % with radars.
- routines: when things first happen on a day, workdays and weekends apart.
- dependencies: event B follows event A within two minutes far more often than chance.
- corrections: something an automation (ours, the owner's, Node-RED) set and a person changed back.
- timeouts: an automation that switches a light off after its room's presence sensor went quiet, the
  timeout it uses and the one the owner's corrections ask for (prototype E2: 12.9 → 2.0 a day).
- usual states and event relevance (core/predict.py) over the longer history, health, modes, asks.
"""

from __future__ import annotations

import bisect
import math
import random
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

from app.core import predict
from app.core.habits import ACT_DOMAINS, SKIP_STATES, relevant
from app.ha.registry import Registry

LEARN_DAYS = 90  # history the layers read
HABIT_DAYS_MAX = 42  # the D35 scan is pure Python: at most six weeks
MAX_LIVE = 16  # live targets, strongest first
MIN_POSITIVES = 6
P_BINS = (0.02, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
DEP_WINDOW_S = 120.0
DEP_MIN_COUNT, DEP_MIN_LIFT, DEP_Z = 5, 3.0, 3.0
MAX_DEPENDENCIES = 40
ROUTINE_MIN_DAYS, ROUTINE_MAX_SPREAD_MIN, ROUTINE_MIN_SHARE = 5, 60.0, 0.5
MAX_ROUTINES = 30
DAY_STARTS_H = 4  # a "day" for routines runs 04:00 → 04:00
CORRECTION_MIN_FIRES, CORRECTION_MIN = 5, 2
TIMEOUT_MIN_OFFS = 5
TIMEOUT_GRID_MIN = range(1, 31)
TIMEOUT_REACT_S = 120.0  # a person switching the light back on this soon after an automatic off
E_MIN = 0.05  # cost of a minute of light in an empty room (20 min = one wrong action)
PRESENCE_CLASSES = frozenset({"occupancy", "motion", "presence"})
PRESENCE_NAME = ("occupancy", "motion", "presence")
FLAP_MIN_PER_DAY, FLAP_FACTOR = 50.0, 5.0
MODE_DOMAINS = frozenset({"input_boolean", "input_select"})


# --- the house as predict.py and habits.py read it ---------------------------------------------------
class HouseView:
    """The registry document (contracts/schemas/house_registry.schema.json) as an `ha` object."""

    def __init__(self, doc: dict | None) -> None:
        doc = doc or {}
        areas = {a["area_id"]: a for a in doc.get("areas") or [] if a.get("area_id")}
        registry = Registry(areas=areas)
        self.states: dict[str, dict] = {}
        for e in doc.get("entities") or []:
            entity_id, area = e["entity_id"], e.get("area_id")
            registry.entity_area[entity_id] = area if area in areas else None
            registry.entity_meta[entity_id] = {**(e.get("registry") or {}), "name": e.get("name")}
            self.states[entity_id] = {
                "entity_id": entity_id,
                "state": e.get("state"),
                "attributes": dict(e.get("attributes") or {}),
            }
        self.registry = registry
        self.ours = {
            entity: {
                "ha_id": info.get("ha_id"),
                "ask": bool(info.get("ask")),
                "targets": {tuple(t) for t in info.get("targets") or [] if len(t) == 2},
            }
            for entity, info in (doc.get("ours") or {}).items()
        }
        self.policy = doc.get("policy")
        settings = doc.get("settings") or {}
        self.habit_days = int(settings.get("habit_days") or 14)
        self.min_days = int(settings.get("habit_min_repeats") or 3)

    def device_class(self, entity_id: str) -> str | None:
        return (self.states.get(entity_id) or {}).get("attributes", {}).get("device_class")


def build_lines(changes) -> dict[str, predict.Line]:
    rows: dict[str, list] = defaultdict(list)
    for c in changes:
        if c.state not in SKIP_STATES:
            rows[c.entity_id].append((c.ts, c.state))
    return {e: predict.Line(r) for e, r in rows.items()}


def append(line: predict.Line, t: float, v: str) -> None:
    """A live change into a line (in order almost always)."""
    if not line.ts or t >= line.ts[-1]:
        line.ts.append(t)
        line.values.append(v)
    else:
        i = bisect.bisect_right(line.ts, t)
        line.ts.insert(i, t)
        line.values.insert(i, v)


# --- live: the booster ---------------------------------------------------------------------------------
def p_bin(p: float) -> int:
    return bisect.bisect_right(P_BINS, p)


@dataclass
class LiveModel:
    entity_id: str
    state: str
    features: list[tuple]
    model: HistGradientBoostingClassifier
    record: dict[int, dict] = field(default_factory=dict)  # bin → {fires, right, days}
    cost: float = predict.COST_PRIOR
    by_hand: int = 0

    def row(self, t: float, lines) -> np.ndarray:
        return np.array([[predict.value(f, t, lines) for f in self.features]], dtype=np.float32)

    def suggest(self, t: float, lines) -> dict | None:
        """What it thinks now, or None when the device already is in that state."""
        current = lines[self.entity_id].at(t)[0] if self.entity_id in lines else None
        if current is None or current == self.state:
            return None
        p_model = float(self.model.predict_proba(self.row(t, lines))[0, 1])
        r = self.record.get(p_bin(p_model)) or {"fires": 0, "right": 0, "days": set()}
        p = (r["right"] + predict.SHADOW_PRIOR * p_model) / (r["fires"] + predict.SHADOW_PRIOR)
        earned = r["fires"] >= predict.MIN_FIRES and len(r["days"]) >= predict.MIN_RIGHT_DAYS
        act = p - (1 - p) * self.cost
        ask = p * predict.ASK_GAIN - (1 - p) * predict.ASK_COST
        mode = "wait" if not earned else "act" if act > 0 else "ask" if ask > 0 else "wait"
        return {
            "entity_id": self.entity_id,
            "state": self.state,
            "p": round(p, 3),
            "p_model": round(p_model, 3),
            "fires": r["fires"],
            "right": r["right"],
            "right_days": len(r["days"]),
            "cost": round(self.cost, 2),
            "mode": mode,
            "earned": earned,
        }

    def describe(self, room: str | None) -> dict:
        fires = sum(r["fires"] for r in self.record.values())
        right = sum(r["right"] for r in self.record.values())
        days = set().union(*(r["days"] for r in self.record.values())) if self.record else set()
        return {
            "entity_id": self.entity_id,
            "state": self.state,
            "room": room,
            "by_hand": self.by_hand,
            "fires": fires,
            "right": right,
            "right_days": len(days),
            "cost": round(self.cost, 2),
        }


def _booster() -> HistGradientBoostingClassifier:
    # the prototype's settings (sim/proto_learner.py): small trees, a little regularisation
    return HistGradientBoostingClassifier(
        max_iter=100, learning_rate=0.08, max_leaf_nodes=15, min_samples_leaf=20, l2_regularization=1.0,
        early_stopping=False, random_state=0,
    )  # fmt: skip


def _fit(X, y, w):
    if y.min() == y.max():
        return None
    model = _booster()
    model.fit(X, y, sample_weight=w)
    return model


def _p(model, X) -> np.ndarray:
    return model.predict_proba(X)[:, 1] if model is not None else np.zeros(len(X))


def samples(target: str, state: str, lines, features, positives, start: float, end: float) -> list[tuple]:
    """(t, label, day) exactly as core/predict.py `evaluate` samples: the 5-minute grid plus right after
    every change of a context entity, while the device is not in the target state."""
    line = lines[target]
    first = math.ceil(start / predict.GRID_S) * predict.GRID_S
    times = {float(t) for t in range(int(first), int(end - predict.HORIZON_S), int(predict.GRID_S))}
    for f in features:
        if f[0] == "since" and f[1] != target:
            times.update(t + 0.001 for t in lines[f[1]].ts if start <= t < end - predict.HORIZON_S)
    pos = sorted(positives)
    out = []
    for t in sorted(times):
        current = line.at(t)[0]
        if current is None or current == state:
            continue
        j = bisect.bisect_right(pos, t)
        out.append((t, int(j < len(pos) and pos[j] <= t + predict.HORIZON_S), predict._local(t).date()))
    return out


def fit_live(target, state, lines, features, positives, start, end, cost, by_hand) -> LiveModel | None:
    rows = samples(target, state, lines, features, positives, start, end)
    labels = [y for _, y, _ in rows]
    days = sorted({d for _, _, d in rows})
    if sum(labels) < MIN_POSITIVES or len(days) < 3:
        return None
    X = np.array([[predict.value(f, t, lines) for f in features] for t, _, _ in rows], dtype=np.float32)
    y = np.array(labels, dtype=np.int8)
    fold_of = {d: i % min(predict.FOLDS, len(days)) for i, d in enumerate(days)}
    fold = np.array([fold_of[d] for _, _, d in rows])
    rng = random.Random(f"{target}:{state}")
    keep = np.array([bool(v) or rng.random() < predict.NEG_RATE for v in labels])
    w = np.where(y == 1, 1.0, 1.0 / predict.NEG_RATE)
    oof = np.zeros(len(rows))
    for k in set(fold_of.values()):
        train = keep & (fold != k)
        oof[fold == k] = _p(_fit(X[train], y[train], w[train]), X[fold == k])
    final = _fit(X[keep], y[keep], w[keep])
    if final is None:
        return None
    key = [p_bin(p) for p in _p(final, X)]
    record: dict[int, dict] = defaultdict(lambda: {"fires": 0, "right": 0, "days": set()})
    line = lines[target]
    run, quiet, fired_at = None, 0.0, None
    for i, (t, label, d) in enumerate(rows):  # predict.evaluate's shadow ladder, keyed by bin
        last = line.at(t)[1]
        if fired_at is not None:
            if last is None or last <= fired_at:
                continue
            fired_at = None
        if t < quiet or (key[i], last) == run or oof[i] < predict.FIRE_P:
            continue
        run = (key[i], last)
        r = record[key[i]]
        r["fires"] += 1
        if label:
            r["right"] += 1
            r["days"].add(d)
            fired_at = t
        else:
            quiet = t + predict.SUPPRESS_S
    return LiveModel(target, state, features, final, dict(record), cost, by_hand)


def train_live(changes, view: HouseView, lines, start: float, end: float, fires) -> list[LiveModel]:
    candidates, _ = relevant(changes, view, min_days=view.min_days, policy=view.policy)
    ours = view.ours
    kept, cost_obs = predict.feedback(
        changes, set(ours), frozenset(e for e, i in ours.items() if i.get("ask"))
    )
    did_after = predict.ask_positives(fires, ours, changes)
    models = []
    for cand in candidates[:MAX_LIVE]:
        target = cand["entity_id"]
        features = predict.context(target, lines, view, max(1, round((end - start) / 86400)))
        manual = [c for c in changes if c.entity_id == target and c.manual]
        for state in sorted({c.state for c in manual} - SKIP_STATES):
            if predict.service(target, state) is None:
                continue
            by_hand = [c.ts for c in manual if c.state == state]
            accepted = [t for t, e, s in kept if e == target and s == state]
            positives = by_hand + accepted + did_after.get((target, state), [])
            cost = predict.cost_of(cost_obs.get((target, state), []))
            model = fit_live(target, state, lines, features, positives, start, end, cost, len(by_hand))
            if model is not None:
                models.append(model)
    return models


# --- routines ------------------------------------------------------------------------------------------
def _minute_of_day(ts: float) -> float:
    moment = predict._local(ts)
    minutes = moment.hour * 60 + moment.minute
    return minutes + 1440 if moment.hour < DAY_STARTS_H else minutes


def _day_key(ts: float):
    moment = predict._local(ts - DAY_STARTS_H * 3600)
    return moment.date(), moment.weekday() >= 5


def routines(changes, view: HouseView, start: float, end: float) -> list[dict]:
    """The first time each kind of event happens on a day: a device switched by hand, a mode, a
    sensor of presence or a bed. Kept when it happens on most days of its kind at about the same time."""
    first: dict[tuple, dict] = defaultdict(dict)  # (entity, state, weekend) → day → minute
    for c in changes:
        if c.state in SKIP_STATES:
            continue
        domain = c.entity_id.split(".", 1)[0]
        if domain in ACT_DOMAINS and not c.manual:
            continue
        if domain == "binary_sensor":
            presence = view.device_class(c.entity_id) in PRESENCE_CLASSES
            if not presence and not any(n in c.entity_id for n in PRESENCE_NAME + ("bed",)):
                continue
        elif domain not in ACT_DOMAINS | MODE_DOMAINS | {"person"}:
            continue
        day, weekend = _day_key(c.ts)
        first[(c.entity_id, c.state, weekend)].setdefault(day, _minute_of_day(c.ts))
    span_days: dict[bool, int] = Counter()
    t = start
    while t < end:
        span_days[_day_key(t)[1]] += 1
        t += 86400
    out = []
    for (entity_id, state, weekend), by_day in first.items():
        if len(by_day) < ROUTINE_MIN_DAYS:
            continue
        minutes = sorted(by_day.values())
        q1, q3 = minutes[len(minutes) // 4], minutes[(3 * len(minutes)) // 4]
        spread = (q3 - q1) / 2
        share = len(by_day) / max(span_days[weekend], 1)
        if spread > ROUTINE_MAX_SPREAD_MIN or share < ROUTINE_MIN_SHARE:
            continue
        median = int(statistics.median(minutes)) % 1440
        out.append({
            "entity_id": entity_id, "state": state, "days": "weekends" if weekend else "workdays",
            "at": f"{median // 60:02d}:{median % 60:02d}", "spread_min": round(spread, 1),
            "seen_days": len(by_day), "share": round(min(share, 1.0), 2),
        })  # fmt: skip
    out.sort(key=lambda r: (-r["share"], r["spread_min"]))
    return out[:MAX_ROUTINES]


# --- dependencies --------------------------------------------------------------------------------------
def dependencies(changes, start: float, end: float) -> list[dict]:
    """B (a device switched by hand) after A (any change) within `DEP_WINDOW_S`, against chance.
    Each A counts once per B kind; expected = A's count × B's rate × the window; kept with a lift of
    `DEP_MIN_LIFT`, at least `DEP_MIN_COUNT` times and `DEP_Z` standard deviations above chance."""
    span = max(end - start, 1.0)
    values: dict[str, set] = defaultdict(set)
    for c in changes:
        values[c.entity_id].add(c.state)
    hands = [
        c for c in changes
        if c.manual and c.state not in SKIP_STATES and c.entity_id.split(".", 1)[0] in ACT_DOMAINS
    ]  # fmt: skip
    hand_ts = [c.ts for c in hands]
    n_b = Counter((c.entity_id, c.state) for c in hands)
    n_a: Counter = Counter()
    pairs: dict[tuple, list[float]] = defaultdict(list)
    for a in changes:
        if a.state in SKIP_STATES or len(values[a.entity_id]) > predict.MAX_VALUES:
            continue
        n_a[(a.entity_id, a.state)] += 1
        seen = set()
        i = bisect.bisect_right(hand_ts, a.ts)
        while i < len(hands) and hands[i].ts <= a.ts + DEP_WINDOW_S:
            b = hands[i]
            kind = (b.entity_id, b.state)
            if b.entity_id != a.entity_id and kind not in seen:
                seen.add(kind)
                pairs[((a.entity_id, a.state, a.manual), kind)].append(b.ts - a.ts)
            i += 1
    out = []
    for ((ae, astate, amanual), (be, bstate)), lags in pairs.items():
        count = len(lags)
        expected = n_a[(ae, astate)] * n_b[(be, bstate)] / span * DEP_WINDOW_S
        if count < DEP_MIN_COUNT or expected <= 0:
            continue
        lift = count / expected
        if lift < DEP_MIN_LIFT or (count - expected) < DEP_Z * math.sqrt(expected):
            continue
        out.append({
            "after": {"entity_id": ae, "state": astate, "by_hand": amanual},
            "then": {"entity_id": be, "state": bstate, "by_hand": True},
            "count": count, "expected": round(expected, 2), "lift": round(lift, 1),
            "lag_s": round(statistics.median(lags), 1), "share": round(count / n_b[(be, bstate)], 2),
        })  # fmt: skip
    out.sort(key=lambda d: -(d["count"] * math.log(d["lift"])))
    return out[:MAX_DEPENDENCIES]


# --- corrections and timeouts --------------------------------------------------------------------------
def _source(c) -> str | None:
    if c.cause in ("automation", "script"):
        return c.ref or c.cause
    if c.cause == "agent":
        return f"agent:{c.ref or 'agent_house'}"
    return None


def corrections(changes, days: float) -> list[dict]:
    """An automated change a person reversed sooner than the state usually lasts (core/predict.py
    `undo_limits`): per source, device and state."""
    by_entity: dict[str, list] = defaultdict(list)
    for c in changes:
        if c.state not in SKIP_STATES:
            by_entity[c.entity_id].append(c)
    stats: dict[tuple, dict] = defaultdict(lambda: {"fires": 0, "delays": []})
    for entity_id, rows in by_entity.items():
        if entity_id.split(".", 1)[0] not in ACT_DOMAINS:
            continue
        limits = predict.undo_limits(rows)
        for i, c in enumerate(rows):
            source = _source(c)
            if source is None:
                continue
            item = stats[(source, entity_id, c.state)]
            item["fires"] += 1
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            limit = limits.get(c.state, predict.UNDO_S)
            if nxt and nxt.manual and nxt.state != c.state and nxt.ts - c.ts <= limit:
                item["delays"].append(nxt.ts - c.ts)
    out = [
        {
            "source": s,
            "entity_id": e,
            "state": st,
            "fires": v["fires"],
            "corrected": len(v["delays"]),
            "per_day": round(len(v["delays"]) / max(days, 1), 2),
            "delay_s": round(statistics.median(v["delays"]), 1),
        }
        for (s, e, st), v in stats.items()
        if v["fires"] >= CORRECTION_MIN_FIRES and len(v["delays"]) >= CORRECTION_MIN
    ]
    out.sort(key=lambda r: -r["corrected"])
    return out


def _presence(view: HouseView, area: str | None, entities) -> list[str]:
    if not area:
        return []
    return sorted(
        e for e in entities
        if e.startswith("binary_sensor.") and view.registry.area_of(e) == area
        and (view.device_class(e) in PRESENCE_CLASSES or any(n in e for n in PRESENCE_NAME))
    )  # fmt: skip


def _any_on(lines, ids: list[str]) -> list[tuple[float, bool]]:
    """When 'any of these is on' changes: [(t, on)]."""
    events = sorted((t, e, v) for e in ids for t, v in zip(lines[e].ts, lines[e].values, strict=True))
    current: dict[str, bool] = {}
    out, was = [], None
    for t, e, v in events:
        current[e] = v == "on"
        now = any(current.values())
        if now != was:
            out.append((t, now))
            was = now
    return out


def timeouts(changes, view: HouseView, lines, days: float) -> list[dict]:
    """The light controller's off-timeout, learnt from the owner's corrections (prototype E2).

    A light whose automatic offs follow its room going quiet at a steady delay has a timeout T0. Every
    quiet stretch of the room while the light was on is labelled: one that reached T0 switched the light
    off, and a person switching it back on within two minutes says someone was still there (a radar
    dropout); a shorter one is labelled by context — someone appearing in another room or at the door
    in its first three minutes means they walked out. The timeout that minimises (wrong offs × the
    learnt cost + empty lit minutes × `E_MIN`) over these stretches is the suggestion."""
    by_entity: dict[str, list] = defaultdict(list)
    for c in changes:
        if c.state not in SKIP_STATES:
            by_entity[c.entity_id].append(c)
    elsewhere_rises: dict[str | None, list[float]] = defaultdict(list)
    all_presence = [
        e
        for e in lines
        if e.startswith("binary_sensor.")
        and (
            view.device_class(e) in PRESENCE_CLASSES | {"door"}
            or any(n in e for n in (*PRESENCE_NAME, "door"))
        )
    ]
    for e in all_presence:
        line = lines[e]
        for i, (t, v) in enumerate(zip(line.ts, line.values, strict=True)):
            if v == "on" and (i == 0 or line.values[i - 1] != "on"):
                elsewhere_rises[view.registry.area_of(e)].append(t)
    out = []
    for entity_id, rows in by_entity.items():
        if entity_id.split(".", 1)[0] not in ("light", "switch"):
            continue
        area = view.registry.area_of(entity_id)
        sensors = [e for e in _presence(view, area, lines) if e in lines]
        if not sensors:
            continue
        offs = [c for c in rows if c.state == "off" and _source(c)]
        if len(offs) < TIMEOUT_MIN_OFFS:
            continue
        quiet = _any_on(lines, sensors)
        q_ts = [t for t, _ in quiet]
        delays, sources = [], Counter()
        for c in offs:
            i = bisect.bisect_right(q_ts, c.ts) - 1
            if i >= 0 and not quiet[i][1]:
                delays.append(c.ts - quiet[i][0])
                sources[_source(c)] += 1
        if len(delays) < TIMEOUT_MIN_OFFS:
            continue
        delays.sort()
        t0 = statistics.median(delays)
        q1, q3 = delays[len(delays) // 4], delays[(3 * len(delays)) // 4]
        if t0 < 30 or (q3 - q1) > max(0.5 * t0, 60):
            continue  # not a steady timeout
        light = lines.get(entity_id)
        if light is None:
            continue
        others = sorted(t for a, ts in elsewhere_rises.items() if a != area for t in ts)
        manual_on = sorted(c.ts for c in rows if c.state == "on" and c.manual)
        labelled = []  # (length_s, someone still there)
        for i, (t, on) in enumerate(quiet):
            if on or light.at(t)[0] != "on":
                continue
            end = quiet[i + 1][0] if i + 1 < len(quiet) else None
            if end is None:
                continue
            length = end - t
            if length >= t0:
                j = bisect.bisect_left(manual_on, t + t0)
                still = j < len(manual_on) and manual_on[j] <= t + t0 + TIMEOUT_REACT_S
            else:
                k = bisect.bisect_left(others, t)
                still = not (k < len(others) and others[k] <= t + min(length, 180))
            labelled.append((length, still))
        if len(labelled) < TIMEOUT_MIN_OFFS:
            continue
        c_off = predict.COST_PRIOR

        def cost(T: float, labelled=labelled, c_off=c_off) -> float:
            return sum(
                (c_off if length >= T else 0.0) if still else E_MIN * min(T, length) / 60
                for length, still in labelled
            )

        best = min(TIMEOUT_GRID_MIN, key=lambda m: (cost(m * 60), m))
        corrected = sum(1 for length, still in labelled if still and length >= t0)
        current = round(t0 / 60, 1)
        per_day = max(days, 1)
        out.append(
            {
                "entity_id": entity_id,
                "presence": sensors,
                "source": sources.most_common(1)[0][0],
                "current_min": current,
                "suggested_min": best,
                "corrections_per_day": round(corrected / per_day, 2),
                "cost_now": round(cost(t0) / per_day, 2),
                "cost_suggested": round(cost(best * 60) / per_day, 2),
                "runs": len(labelled),
            }
        )
    return out


# --- health, modes, asks -------------------------------------------------------------------------------
def health(changes, view: HouseView, end: float) -> dict:
    last_change: dict[str, float] = {}
    per_day: dict[str, Counter] = defaultdict(Counter)
    for c in changes:
        last_change[c.entity_id] = c.ts
        if c.entity_id.split(".", 1)[0] in ("binary_sensor", "switch", "light"):
            per_day[c.entity_id][int(c.ts // 86400)] += 1
    unavailable = sorted(
        ({"entity_id": e, "since": last_change.get(e)} for e, s in view.states.items()
         if s.get("state") == "unavailable" and not view.registry.is_hidden(e)),
        key=lambda u: u["since"] or 0,
    )  # fmt: skip
    today = int(end // 86400)
    flapping = []
    for e, days in per_day.items():
        recent = sum(days.get(d, 0) for d in range(today - 2, today + 1)) / 3
        usual = statistics.median(days.values()) if len(days) >= 7 else None
        if usual is not None and recent >= FLAP_MIN_PER_DAY and recent >= FLAP_FACTOR * usual:
            flapping.append({"entity_id": e, "per_day": round(recent, 1), "usual_per_day": round(usual, 1)})
    return {"unavailable": unavailable[:50], "flapping": flapping[:20]}


def modes(changes, view: HouseView, predicted: set[str]) -> list[dict]:
    out = []
    for e in sorted(view.states):
        domain = e.split(".", 1)[0]
        if domain not in MODE_DOMAINS and not (domain in ("switch", "input_boolean") and "mode" in e):
            continue
        rows = [c for c in changes if c.entity_id == e and c.state not in SKIP_STATES]
        if not rows:
            continue
        out.append({"entity_id": e, "states": sorted({c.state for c in rows}),
                    "by_hand": sum(c.manual for c in rows), "predicted": e in predicted})  # fmt: skip
    return out


def asks(fires: dict[str, list[float]], answers: list[tuple[float, str]], view: HouseView) -> dict:
    fired = sum(len(fires.get(e, [])) for e, info in view.ours.items() if info.get("ask"))
    no = sum(1 for _, action in answers if action.endswith("_NO"))
    return {"fired": fired, "yes": len(answers) - no, "no": no}


# --- everything ----------------------------------------------------------------------------------------
def train(journal, registry_doc: dict | None, now: float | None = None) -> tuple[dict, list[LiveModel], dict]:
    """(house model document, live models, lines for live scoring)."""
    started = time.time()
    now = now or time.time()
    view = HouseView(registry_doc)
    stats = journal.stats()
    first = stats["first_ts"] or now
    start = max(first, now - LEARN_DAYS * 86400)
    days = max((now - start) / 86400, 1 / 24)
    changes = journal.changes(start, now)
    fires = journal.fires(start, now)
    lines = build_lines(changes)
    habit_days = max(view.habit_days, min(HABIT_DAYS_MAX, math.floor(days)))
    habit_start = now - habit_days * 86400
    habit_changes = [c for c in changes if c.ts >= habit_start]
    preds, funnel = predict.scan(
        habit_changes, view, days=habit_days, min_days=view.min_days, policy=view.policy,
        ours=view.ours, end=now, fires=fires,
    )  # fmt: skip
    for p in preds:  # JSON: the rule's features are tuples
        p["path"] = [{**step, "f": list(step["f"])} for step in p.get("path", [])]
    live = train_live(changes, view, lines, start, now, fires)
    predicted = {p["entity_id"] for p in preds} | {m.entity_id for m in live}
    doc = {
        "schema_version": 1,
        "trained_at": now,
        "history": {"first_ts": stats["first_ts"], "last_ts": stats["last_ts"], "days": round(days, 2),
                    "events": len(changes), "by_hand": sum(1 for c in changes if c.manual
                                                           and c.entity_id.split(".", 1)[0] in ACT_DOMAINS)},
        "habits": {"days": habit_days, "predictions": preds, "funnel": funnel},
        "live": [m.describe(view.registry.area_of(m.entity_id)) for m in live],
        "routines": routines(changes, view, start, now),
        "dependencies": dependencies(changes, start, now),
        "usual_states": predict.usual_states(changes, view, days=math.ceil(days), min_days=view.min_days,
                                             policy=view.policy, end=now),
        "event_relevance": predict.event_relevance(changes, view),
        "health": health(changes, view, now),
        "corrections": corrections(changes, days),
        "timeouts": timeouts(changes, view, lines, days),
        "modes": modes(changes, view, predicted),
        "asks": asks(fires, journal.answers(start, now), view),
    }  # fmt: skip
    doc["duration_s"] = round(time.time() - started, 2)
    return doc, live, lines
