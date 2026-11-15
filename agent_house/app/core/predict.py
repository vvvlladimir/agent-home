"""Habits as predictions (D35). Deterministic, no model, pure Python (the image has no numpy).

The question is not "what recurs" but "would the owner do this now, and is it worth doing for
them". Per device and target state (light.x → on):

1. Context, the same recipe for every entity: of each entity in the device's area and of the
   house-wide people, sun and helpers, whether it is in each of its few usual states and how long
   since it changed; the hour; workday or weekend. Samples are taken every `GRID_S` and right after
   every change of a context entity (that is where triggers live), while the device is not already in
   the target state. A sample is positive when the owner set it by hand within `HORIZON_S`, or when
   one of our automations did and the owner kept it (an accepted ask, an act not undone). Every other
   sample is a negative: the moments the owner did *not* act are what time automations never saw.
2. Model: a shallow decision tree (depth `DEPTH`); each leaf is a rule that maps onto Home
   Assistant triggers and conditions.
3. Shadow record, out of sample: the days are split into folds by day. On each fold, the tree
   trained without it decides whether to fire (its P ≥ `FIRE_P`, once per rule and stretch of the
   device's state; after a right fire it waits for the device to change, after a wrong one
   `SUPPRESS_S`), and the fire is booked on the rule (leaf) of the final tree the moment falls in. A
   fire is right when the owner did it within `HORIZON_S`. A rule needs `MIN_FIRES` fires, right on
   `MIN_RIGHT_DAYS` days, before it may ask or act.
4. Decision by expected utility (Horvitz, mixed-initiative): with P = the shadow precision shrunk
   towards the tree's own estimate, act when P > c / (1 + c) (c = the learnt cost of a wrong action of
   this device and state, prior `COST_PRIOR`, learnt from how fast the owner undid ours); ask (a
   phone notification with a button) when P·ASK_GAIN > (1 − P)·ASK_COST, within `ASK_BUDGET` asks a
   day for the whole house; else wait. Nothing here is per domain: a TV's "on" is not proposed
   because it is unpredictable, not because it is a TV.

A proposal is built here, deterministically: one automation per device and state, the best rule
as triggers + conditions. An ask that the owner keeps accepting climbs to act on a later scan
(the accepted changes are positives), superseding the ask automation. An ask the owner ignores
and then does by hand is cleared from the phone (the automation waits for the button *or* the
device reaching the state) and counts as right: the moment it asked becomes a positive, so the
next scan is surer of it, not less.
"""

from __future__ import annotations

import bisect
import math
import random
from collections import defaultdict
from datetime import datetime

from .habits import ACT_DOMAINS, SKIP_STATES, relevant

HORIZON_S = 180.0
GRID_S = 300.0
SINCE_MIN = (2, 5, 15, 60, 180)
DEPTH = 3
MIN_LEAF = 4  # samples
NEG_RATE = 0.05  # negatives kept for training, weighted back up
FOLDS = 5
SHADOW_PRIOR = 2.0  # the tree's own P counts as this many shadow fires
MIN_FIRES = 4
MIN_RIGHT_DAYS = 3
SUPPRESS_S = 3600.0
COST_PRIOR, COST_PRIOR_N = 3.0, 2.0
COST_FAST, COST_SLOW, FAST_UNDO_S = 4.0, 2.0, 120.0
UNDO_S = 600.0
UNDO_SHARE = 0.25
ASK_GAIN, ASK_COST = 0.5, 0.3
FIRE_P = ASK_COST / (ASK_GAIN + ASK_COST)  # a fold tree fires (in shadow) from the ask threshold up
ASK_BUDGET = 3.0  # asks a day, all rules together
ASK_TIMEOUT_MIN = 10
NOTIFY = "notify.notify"  # [UNVERIFIED-ON-HW] HA's default group of the companion apps
ASK_EVENT = "mobile_app_notification_action"
MAX_CONTEXT = 40
MAX_VALUES = 6  # an entity with more distinct states is continuous or noise: not context
CHATTY_PER_DAY = 300
MAX_PREDICTIONS = 8
COVERED_SHARE = 0.8
HOUSE_WIDE = frozenset({"person", "sun", "input_boolean", "input_select"})
NO_CONTEXT = frozenset({"automation", "script", "update", "button", "scene", "event", "input_button"})
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]


def service(entity_id: str, state: str) -> tuple[str, dict] | None:
    """The service that sets a device to a state, or None (then the state is not proposed)."""
    domain = entity_id.split(".", 1)[0]
    if domain == "cover" and state in ("open", "closed"):
        return ("cover.open_cover" if state == "open" else "cover.close_cover"), {}
    if domain == "climate" and state in ("off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only"):
        return "climate.set_hvac_mode", {"hvac_mode": state}
    if domain == "input_select":  # the house mode helper too: the owner's own switches are habits
        return "input_select.select_option", {"option": state}
    if state in ("on", "off") and domain in ACT_DOMAINS:
        return f"{domain}.turn_{state}", {}
    return None


def acted_states(text: str) -> set[tuple[str, str]]:
    """(entity, state) pairs an automation's actions set, as far as `service` can tell."""
    import yaml  # local: only the scan needs it

    try:
        config = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return set()
    out: set[tuple[str, str]] = set()

    def walk(steps) -> None:
        for step in steps if isinstance(steps, list) else [steps]:
            if not isinstance(step, dict):
                continue
            svc = step.get("action") or step.get("service")
            if isinstance(svc, str) and "." in svc:
                name = svc.split(".", 1)[1]
                data = step.get("data") if isinstance(step.get("data"), dict) else {}
                state = {
                    "turn_on": "on",
                    "turn_off": "off",
                    "open_cover": "open",
                    "close_cover": "closed",
                }.get(name, data.get("hvac_mode") if name == "set_hvac_mode" else data.get("option"))
                ids = (
                    (step.get("target") or {}).get("entity_id")
                    if isinstance(step.get("target"), dict)
                    else None
                )
                for e in ids if isinstance(ids, list) else [ids]:
                    if isinstance(e, str) and state:
                        out.add((e, state))
            for key in ("then", "else", "sequence", "default"):
                if key in step:
                    walk(step[key])
            for option in step.get("choose") or [] if isinstance(step.get("choose"), list) else []:
                walk((option or {}).get("sequence", []))

    walk(config.get("actions") or config.get("action") or [])
    return out


# --- timelines and features ------------------------------------------------------------------------
class Line:
    def __init__(self, rows: list[tuple[float, str]]) -> None:
        self.ts = [t for t, _ in rows]
        self.values = [v for _, v in rows]

    def at(self, t: float) -> tuple[str | None, float | None]:
        i = bisect.bisect_right(self.ts, t) - 1
        return (self.values[i], self.ts[i]) if i >= 0 else (None, None)


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts).astimezone()


def _since_bin(seconds: float | None) -> int:
    if seconds is None:
        return len(SINCE_MIN)
    minutes = seconds / 60
    return next((i for i, b in enumerate(SINCE_MIN) if minutes < b), len(SINCE_MIN))


def value(feature: tuple, t: float, lines: dict[str, Line]) -> int:
    kind = feature[0]
    if kind == "is":
        return int(lines[feature[1]].at(t)[0] == feature[2])
    if kind == "since":
        last = lines[feature[1]].at(t)[1]
        return _since_bin(None if last is None else t - last)
    moment = _local(t)
    return moment.hour if kind == "hour" else int(moment.weekday() >= 5)


def context(target: str, lines: dict[str, Line], ha, days: int) -> list[tuple]:
    """Features for one device: its area's entities and the house-wide ones, the same recipe each."""
    area = ha.registry.area_of(target)
    picked = []
    for entity_id, line in lines.items():
        domain = entity_id.split(".", 1)[0]
        if domain in NO_CONTEXT or len(line.ts) > CHATTY_PER_DAY * max(days, 1):
            continue
        if entity_id == target or domain in HOUSE_WIDE or (area and ha.registry.area_of(entity_id) == area):
            picked.append(entity_id)
    picked.sort(key=lambda e: (e != target, -len(lines[e].ts), e))
    features: list[tuple] = []
    for entity_id in picked[:MAX_CONTEXT]:
        counts: dict[str, int] = defaultdict(int)
        for v in lines[entity_id].values:
            counts[v] += 1
        if len(counts) > MAX_VALUES:
            continue
        usual = sorted(counts, key=lambda v: (-counts[v], v))
        if entity_id != target:  # the device's own state is fixed by the sample (not the target)
            if len(usual) == 2:  # ("is", entity, value, the other value): "is not on" reads "is off"
                features.append(("is", entity_id, usual[0], usual[1]))
            else:
                features += [("is", entity_id, v) for v in usual[:3]]
        features.append(("since", entity_id))
    return features + [("hour",), ("weekend",)]


# --- a small weighted decision tree ---------------------------------------------------------------
def _h(p: float, t: float) -> float:
    if t <= 0 or p <= 0 or p >= t:
        return 0.0
    q = p / t
    return -(q * math.log(q) + (1 - q) * math.log(1 - q))


def grow(rows: list[tuple[list[int], int, float]], depth: int) -> dict:
    """rows: (feature values, label, weight)."""
    pos = sum(w for _, y, w in rows if y)
    tot = sum(w for _, _, w in rows)
    node: dict = {"p": (pos + 0.5) / (tot + 1)}
    if depth == 0 or pos == 0 or pos == tot or len(rows) < 2 * MIN_LEAF:
        return node
    best, base = None, _h(pos, tot) * tot
    for f in range(len(rows[0][0])):
        cnt: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0])
        for x, y, w in rows:
            c = cnt[x[f]]
            c[0] += w * y
            c[1] += w
            c[2] += 1
        lp = lt = ln = 0.0
        for k in sorted(cnt)[:-1]:
            lp, lt, ln = lp + cnt[k][0], lt + cnt[k][1], ln + cnt[k][2]
            if ln < MIN_LEAF or len(rows) - ln < MIN_LEAF:
                continue
            gain = base - _h(lp, lt) * lt - _h(pos - lp, tot - lt) * (tot - lt)
            if best is None or gain > best[0]:
                best = (gain, f, k)
    if best is None or best[0] < 1e-3 * tot:
        return node
    _, f, k = best
    node.update(f=f, k=k, l=grow([r for r in rows if r[0][f] <= k], depth - 1),
                r=grow([r for r in rows if r[0][f] > k], depth - 1))  # fmt: skip
    return node


def leaf(node: dict, features: list[tuple], t: float, lines: dict[str, Line]) -> tuple[float, tuple]:
    """(the tree's P, the rule: ((feature, threshold, left), …)) — computes only the features it needs."""
    path = []
    while "f" in node:
        left = value(features[node["f"]], t, lines) <= node["k"]
        path.append((features[node["f"]], node["k"], left))
        node = node["l"] if left else node["r"]
    return node["p"], tuple(path)


def leaves(node: dict, features: list[tuple], path: tuple = ()) -> list[tuple[float, tuple]]:
    if "f" not in node:
        return [(node["p"], path)]
    f, k = features[node["f"]], node["k"]
    return leaves(node["l"], features, path + ((f, k, True),)) + leaves(
        node["r"], features, path + ((f, k, False),)
    )


# --- feedback on our own automations ---------------------------------------------------------------
def undo_limits(rows) -> dict[str, float]:
    """state → how soon a reversal by hand counts as an undo, for one entity's changes (oldest first):
    `UNDO_SHARE` of the median stretch of that state when the owner set it by hand, at most `UNDO_S`
    (no such stretch: `UNDO_S`). Only the owner's own stretches: if every one of ours is reverted
    after two minutes, that is the undo pattern, not the state's usual length."""
    lasts: dict[str, list[float]] = defaultdict(list)
    for c, nxt in zip(rows, rows[1:], strict=False):
        if c.manual:
            lasts[c.state].append(nxt.ts - c.ts)
    return {st: min(UNDO_S, UNDO_SHARE * sorted(v)[len(v) // 2]) for st, v in lasts.items()}


def feedback(
    changes, ours: set[str], asks: frozenset[str] = frozenset()
) -> tuple[list[tuple[float, str, str]], dict[tuple[str, str], list[float]]]:
    """(changes our automations made that the owner kept: positives; cost observations per device and
    state from the ones the owner undid by hand). An undo is a reversal by hand clearly faster than
    the state usually lasts (`UNDO_SHARE` of its median stretch, at most `UNDO_S`): a hallway light
    is always switched off after two minutes, which is use, not an undo. A fast undo costs
    `COST_FAST`, a slower one `COST_SLOW`. A change one of our asks (`asks`) made happened because
    the owner tapped yes: always kept."""
    by_entity: dict[str, list] = defaultdict(list)
    for c in changes:
        by_entity[c.entity_id].append(c)
    kept, costs = [], defaultdict(list)
    for entity_id, rows in by_entity.items():
        limits = undo_limits(rows)
        for i, c in enumerate(rows):
            if c.cause != "automation" or c.ref not in ours:
                continue
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            if (
                c.ref not in asks
                and nxt is not None
                and nxt.manual
                and nxt.state != c.state
                and nxt.ts - c.ts <= limits.get(c.state, UNDO_S)
            ):
                costs[(entity_id, c.state)].append(COST_FAST if nxt.ts - c.ts <= FAST_UNDO_S else COST_SLOW)
            else:
                kept.append((c.ts, entity_id, c.state))
    return kept, costs


def cost_of(observed: list[float]) -> float:
    return (COST_PRIOR * COST_PRIOR_N + sum(observed)) / (COST_PRIOR_N + len(observed))


# --- one device and state --------------------------------------------------------------------------
def evaluate(target: str, state: str, lines, features, positives: list[float], start: float, end: float):
    """(final tree, shadow record per rule) for one device and state; (None, {}) without a positive."""
    line = lines[target]
    first = (
        math.ceil(start / GRID_S) * GRID_S
    )  # on the clock's 5-minute marks: the same samples on every scan
    times = {float(t) for t in range(int(first), int(end - HORIZON_S), int(GRID_S))}
    for f in features:
        if f[0] == "since" and f[1] != target:
            times.update(t + 0.001 for t in lines[f[1]].ts if start <= t < end - HORIZON_S)
    samples = []  # (t, label, day)
    pos = sorted(positives)
    for t in sorted(times):
        current = line.at(t)[0]
        if current is None or current == state:
            continue
        j = bisect.bisect_right(pos, t)
        samples.append((t, int(j < len(pos) and pos[j] <= t + HORIZON_S), _local(t).date()))
    if not any(y for _, y, _ in samples):
        return None, {}
    days = sorted({d for _, _, d in samples})
    fold_of = {d: i % min(FOLDS, len(days)) for i, d in enumerate(days)}
    rng = random.Random(f"{target}:{state}")
    keep = [(t, y, d) for t, y, d in samples if y or rng.random() < NEG_RATE]
    cache: dict[float, list[int]] = {}

    def row(t: float) -> list[int]:
        if t not in cache:
            cache[t] = [value(f, t, lines) for f in features]
        return cache[t]

    def train(skip_fold: int | None) -> dict:
        rows = [(row(t), y, 1.0 if y else 1 / NEG_RATE) for t, y, d in keep
                if skip_fold is None or fold_of[d] != skip_fold]  # fmt: skip
        return grow(rows, DEPTH) if rows else {"p": 0.0}

    trees = {k: train(k) for k in set(fold_of.values())} if len(days) >= 2 else {}
    final = train(None)
    record: dict[tuple, dict] = defaultdict(lambda: {"fires": 0, "right": 0, "days": set()})
    run, quiet, fired_at = None, 0.0, None
    for t, y, d in samples:
        last = line.at(t)[1]
        if fired_at is not None:  # after a right fire: wait until the device changed again
            if last is None or last <= fired_at:
                continue
            fired_at = None
        if t < quiet or not trees:
            continue
        _, key = leaf(final, features, t, lines)
        if (key, last) == run:  # the same rule over the same stretch of the device's state
            continue
        if leaf(trees[fold_of[d]], features, t, lines)[0] < FIRE_P:
            continue  # the tree that never saw this day would not have fired here
        run = (key, last)
        r = record[key]
        r["fires"] += 1
        if y:
            r["right"] += 1
            r["days"].add(d)
            fired_at = t
        else:
            quiet = t + SUPPRESS_S
    return final, record


# --- the scan --------------------------------------------------------------------------------------
def ask_positives(fires: dict[str, list[float]], ours: dict, changes) -> dict[tuple[str, str], list[float]]:
    """An ask the owner ignored and then did by hand within `ASK_TIMEOUT_MIN`: the situation was
    right, so the moment it asked is a positive too (the question was cleared, see `automation`)."""
    out: dict[tuple[str, str], list[float]] = defaultdict(list)
    hands: dict[tuple[str, str], list[float]] = defaultdict(list)
    for c in changes:
        if c.manual:
            hands[(c.entity_id, c.state)].append(c.ts)
    for ref, times in (fires or {}).items():
        info = ours.get(ref) or {}
        if not info.get("ask"):
            continue
        for pair in info.get("targets", ()):
            for t in times:
                if any(t < h <= t + ASK_TIMEOUT_MIN * 60 for h in hands.get(tuple(pair), ())):
                    out[tuple(pair)].append(t + 0.002)
    return out


def scan(
    changes, ha, *, days: int, min_days: int, policy=None, ours: dict | None = None, end=None, fires=None
):
    """(predictions, strongest first; the funnel). `ours`: automation entity → {ha_id, ask, targets}
    for our deployed automations, targets as (entity, state) pairs: a pair an ask of ours sets may
    climb to act; a pair any other automation of ours sets is skipped (the device's other states
    are not: an automation for "on" leaves "off" to be proposed)."""
    ours = ours or {}
    changes = sorted(changes, key=lambda c: c.ts)
    end = end or (changes[-1].ts + 1 if changes else 0.0)
    start = end - days * 86400
    acting = {pair: info for info in ours.values() for pair in info.get("targets", ())}
    candidates, funnel = relevant(changes, ha, min_days=min_days, policy=policy)
    rows: dict[str, list] = defaultdict(list)
    for c in changes:
        if c.state not in SKIP_STATES:
            rows[c.entity_id].append((c.ts, c.state))
    lines = {e: Line(r) for e, r in rows.items()}
    did_after_ask = ask_positives(fires or {}, ours, changes)
    kept, cost_obs = feedback(changes, set(ours), frozenset(e for e, i in ours.items() if i.get("ask")))
    preds, waiting, covered = [], [], []
    for cand in candidates:
        target = cand["entity_id"]
        features = context(target, lines, ha, days)
        states = {c.state for c in changes if c.entity_id == target and c.manual}
        states = sorted((states | {s for _, e, s in kept if e == target}) - SKIP_STATES)
        for state in states:
            asking = acting.get((target, state))
            if service(target, state) is None or (asking and not asking.get("ask")):
                continue
            by_hand = [c.ts for c in changes if c.entity_id == target and c.state == state and c.manual]
            theirs = [c for c in changes if c.entity_id == target and c.state == state
                      and c.cause in ("automation", "script") and c.ref not in ours]  # fmt: skip
            if theirs and len(theirs) >= COVERED_SHARE * (len(theirs) + len(by_hand)):
                covered.append(
                    {"entity_id": target, "state": state, "by": sorted({c.ref or c.cause for c in theirs})}
                )
                continue
            accepted = [t for t, e, s in kept if e == target and s == state]
            did_after = did_after_ask.get((target, state), [])
            tree, record = evaluate(
                target, state, lines, features, by_hand + accepted + did_after, start, end
            )
            if tree is None:
                continue
            cost = cost_of(cost_obs.get((target, state), []))
            best = _decide(tree, features, record, cost, days)
            if best is None:
                waiting.append(f"{target} → {state}")
                continue
            if not best["path"]:  # how long after the device's own change the owner acts
                line = lines[target]
                gaps = sorted(t - (line.at(t - 0.001)[1] or t) for t in by_hand)
                others = [v for v in line.values if v != state]
                best["other"] = max(set(others), key=others.count) if others else "on"
                best["hold_min"] = max(1, round(gaps[len(gaps) // 2] / 60))
            if asking and best["mode"] != "act":
                continue  # our ask stands until it has earned acting
            preds.append({
                "entity_id": target, "state": state, "room": ha.registry.area_of(target),
                "name": ha.registry.name_of(target)
                or (ha.states.get(target) or {}).get("attributes", {}).get("friendly_name"),
                "by_hand": len(by_hand), "accepted": len(accepted), "did_after_ask": len(did_after),
                "cost": round(cost, 2),
                "days_scanned": days, "supersedes": [asking["ha_id"]] if asking else [], **best,
            })  # fmt: skip
    preds.sort(key=lambda p: -p["value"])
    asks, budget, out = 0.0, ASK_BUDGET, []
    for p in preds:
        if p["mode"] == "ask":
            if asks + p["per_day"] > budget:
                waiting.append(f"{p['entity_id']} → {p['state']} (ask budget)")
                continue
            asks += p["per_day"]
        out.append(p)
    for p in out:
        p["rule"] = describe(p["path"], ha, p)
        p["yaml_config"] = automation(p, ha)
    out = out[:MAX_PREDICTIONS]
    funnel.update({
        "predictions": len(out), "dossiers": len(out), "waiting": waiting[:20], "irregular": len(waiting),
        "covered_by_owner": covered, "asks_per_day": round(asks, 2),
    })  # fmt: skip
    if covered:
        funnel["left_out"]["the owner's own automation already does it"] = len(covered)
    return out, funnel


def _decide(tree, features, record, cost: float, days: int) -> dict | None:
    """The rule of the final tree worth the most a day, with its mode, or None (wait)."""
    best = None
    for p_tree, path in leaves(tree, features):
        r = record.get(path)
        if r is None or r["fires"] < MIN_FIRES or len(r["days"]) < MIN_RIGHT_DAYS:
            continue
        p = (r["right"] + SHADOW_PRIOR * p_tree) / (r["fires"] + SHADOW_PRIOR)
        per_day = r["fires"] / max(days, 1)
        act, ask = p - (1 - p) * cost, p * ASK_GAIN - (1 - p) * ASK_COST
        if act > 0:
            mode, gain = "act", act
        elif ask > 0:
            mode, gain = "ask", ask
        else:
            continue
        item = {"mode": mode, "p": round(p, 2), "fires": r["fires"], "right": r["right"],
                "right_days": len(r["days"]), "per_day": round(per_day, 2), "value": round(gain * per_day, 3),
                "path": [{"f": list(f), "k": k, "left": left} for f, k, left in path]}  # fmt: skip
        if best is None or item["value"] > best["value"]:
            best = item
    return best


# --- what events matter (the event gate, D35) -------------------------------------------------------
FOLLOW_S = 600.0
QUIET_MIN_EVENTS = 20  # an event kind seen this often …
QUIET_SHARE = 0.05  # … and followed by something the owner did this rarely is "quiet"


def event_relevance(changes, ha) -> dict[str, list[int]]:
    """ "entity|state" → [followed, seen]: how often a change of an entity to a state was followed
    within `FOLLOW_S` by something the owner did by hand to a device in the same area (anywhere for an
    entity without one). Only non-device entities (sensors, people, sun, helpers): those are what
    agents are woken by. Counts, no model; the gate slows down wakes on quiet event kinds."""
    hands_by_area: dict[str | None, list[float]] = defaultdict(list)
    for c in changes:
        if c.manual and c.entity_id.split(".", 1)[0] in ACT_DOMAINS and c.state not in SKIP_STATES:
            hands_by_area[ha.registry.area_of(c.entity_id)].append(c.ts)
    every = sorted(t for ts in hands_by_area.values() for t in ts)
    for ts in hands_by_area.values():
        ts.sort()
    out: dict[str, list[int]] = {}
    for c in changes:
        if c.entity_id.split(".", 1)[0] in ACT_DOMAINS | NO_CONTEXT or c.state in SKIP_STATES:
            continue
        area = ha.registry.area_of(c.entity_id)
        hands = hands_by_area.get(area, []) if area else every
        j = bisect.bisect_right(hands, c.ts)
        item = out.setdefault(f"{c.entity_id}|{c.state}", [0, 0])
        item[1] += 1
        item[0] += int(j < len(hands) and hands[j] <= c.ts + FOLLOW_S)
    return out


def quiet(relevance: dict[str, list[int]], entity_id: str, state: str) -> tuple[int, int] | None:
    """(followed, seen) when this event kind is quiet, else None."""
    item = relevance.get(f"{entity_id}|{state}")
    if item and item[1] >= QUIET_MIN_EVENTS and item[0] < QUIET_SHARE * item[1]:
        return item[0], item[1]
    return None


# --- what states are usual (unusual-state notes, D35) ----------------------------------------------
BLOCK_H = 3  # hours per time-of-day block


def cell(ts: float, home: bool | None) -> str:
    """The context cell of a moment: time-of-day block and whether anybody is home."""
    block = _local(ts).hour // BLOCK_H
    return f"{block}|{'-' if home is None else int(home)}"


def anybody_home(lines: dict[str, Line], t: float) -> bool | None:
    people = [line.at(t)[0] for e, line in lines.items() if e.startswith("person.")]
    known = [p for p in people if p is not None]
    return any(p == "home" for p in known) if known else None


def usual_states(changes, ha, *, days: int, min_days: int, policy=None, end=None) -> dict[str, dict]:
    """entity → {state → {cell → [minutes in that state, minutes seen]}} for the devices the owner
    switches by hand, on a `GRID_S` grid over the scan. Counts, no model: how usual a device's state
    is at this time of day with (or without) anybody home."""
    changes = sorted(changes, key=lambda c: c.ts)
    end = end or (changes[-1].ts + 1 if changes else 0.0)
    start = end - days * 86400
    candidates, _ = relevant(changes, ha, min_days=min_days, policy=policy)
    rows: dict[str, list] = defaultdict(list)
    for c in changes:
        if c.state not in SKIP_STATES:
            rows[c.entity_id].append((c.ts, c.state))
    lines = {e: Line(r) for e, r in rows.items()}
    people = {e: lines[e] for e in lines if e.startswith("person.")}
    first = math.ceil(start / GRID_S) * GRID_S
    grid = [float(t) for t in range(int(first), int(end), int(GRID_S))]
    cells = [cell(t, anybody_home(people, t)) for t in grid]
    out: dict[str, dict] = {}
    for cand in candidates:
        line = lines[cand["entity_id"]]
        table: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        seen: dict[str, int] = defaultdict(int)
        for t, c in zip(grid, cells, strict=True):
            state = line.at(t)[0]
            if state is None:
                continue
            seen[c] += 1
            table[state][c][0] += 1
        minutes = int(GRID_S // 60)
        out[cand["entity_id"]] = {
            state: {c: [n * minutes, seen[c] * minutes] for c, (n, _) in by_cell.items()}
            | {c: [0, seen[c] * minutes] for c in seen if c not in by_cell}
            for state, by_cell in table.items()
        }
    return out


# --- words and YAML --------------------------------------------------------------------------------
def _name(ha, entity_id: str) -> str:
    return ha.registry.name_of(entity_id) or entity_id


def condition_text(c: dict, ha) -> str:
    f, k, left = c["f"], c["k"], c["left"]
    if f[0] == "is":
        if left and len(f) > 3:
            return f"{_name(ha, f[1])} is {f[3]}"
        return f"{_name(ha, f[1])} is {'not ' if left else ''}{f[2]}"
    if f[0] == "since":
        if k >= len(SINCE_MIN):
            return f"{_name(ha, f[1])} {'changed' if left else 'unchanged'}"
        return (f"{_name(ha, f[1])} changed in the last {SINCE_MIN[k]} min" if left
                else f"{_name(ha, f[1])} unchanged for {SINCE_MIN[k]} min")  # fmt: skip
    if f[0] == "hour":
        return f"before {k + 1:02d}:00" if left else f"from {k + 1:02d}:00"
    return "on workdays" if left else "at weekends"


def describe(path: list[dict], ha, p: dict | None = None) -> str:
    if not path and p:  # no condition: the owner always does it a while after the device changed
        return f"{_name(ha, p['entity_id'])} has been {p['other']} for {p['hold_min']} min"
    return " and ".join(condition_text(c, ha) for c in path)


def summary(p: dict) -> str:
    mode = "do it" if p["mode"] == "act" else "ask you first"
    return (
        f"→ {p['state']} when {p['rule']}: you did it within {int(HORIZON_S // 60)} min after "
        f"{p['right']} of {p['fires']} such moments on {p['right_days']} days; would {mode}"
    )


def request(p: dict) -> str:
    """The proposal's rationale: what was counted, and what the automation does."""
    lines = [
        f"{p['name'] or p['entity_id']} → {p['state']}. Counted from Home Assistant's logbook over the last "
        f"{p['days_scanned']} days, no model involved.",
        f"Situation: {p['rule']}.",
        f"On days the rule had not seen (cross-validated by day) it came up {p['fires']} times; you set "
        f"{p['entity_id']} to {p['state']} within {int(HORIZON_S // 60)} min {p['right']} times, on "
        f"{p['right_days']} days (P = {p['p']}). You did it by hand {p['by_hand']} times in all"
        + (f", and accepted our automation {p['accepted']} times" if p["accepted"] else "")
        + (f"; {p.get('did_after_ask')} times you ignored our question and did it yourself" * (
            p.get("did_after_ask", 0) > 0
        ))
        + ".",
        f"A wrong action costs about {p['cost']} (learnt from how fast you undo ours; {COST_PRIOR} until "
        "then), so this " + ("acts: P > cost / (1 + cost)." if p["mode"] == "act"
                             else f"asks first with a notification ({NOTIFY}, button within "
                                  f"{ASK_TIMEOUT_MIN} min): sure enough to ask, not to act."),
    ]  # fmt: skip
    if p.get("supersedes"):
        lines.append(f"You kept accepting the ask, so this replaces {', '.join(p['supersedes'])}.")
    return "\n".join(lines)


def automation(p: dict, ha) -> dict:
    """The rule as one Home Assistant automation (plural keys)."""
    target, state, path = p["entity_id"], p["state"], p["path"]
    is_true = {c["f"][1]: c["f"][2] for c in path if c["f"][0] == "is" and not c["left"]}
    is_true |= {c["f"][1]: c["f"][3] for c in path if c["f"][0] == "is" and c["left"] and len(c["f"]) > 3}
    recent = [
        (c["f"][1], c["k"]) for c in path if c["f"][0] == "since" and c["left"] and c["k"] < len(SINCE_MIN)
    ]
    holds = {c["f"][1]: SINCE_MIN[c["k"]] for c in path
             if c["f"][0] == "since" and not c["left"] and c["k"] < len(SINCE_MIN)}  # fmt: skip
    triggers: list[dict] = []
    # the change that just happened triggers; other recent changes cannot be told without templates
    for entity_id, _ in recent[:1]:
        trig = {"trigger": "state", "entity_id": entity_id}
        if entity_id in is_true:
            trig["to"] = is_true[entity_id]
        triggers.append(trig)
    if not triggers:
        for entity_id, v in is_true.items():
            trig = {"trigger": "state", "entity_id": entity_id, "to": v}
            if entity_id in holds:
                trig["for"] = {"minutes": holds[entity_id]}
            triggers.append(trig)
    conditions: list[dict] = [
        {"condition": "not", "conditions": [{"condition": "state", "entity_id": target, "state": state}]}
    ]
    for c in path:
        f, k, left = c["f"], c["k"], c["left"]
        if f[0] == "is":
            positive = not left or len(f) > 3  # "is not off" of a two-state entity is "is on"
            cond = {"condition": "state", "entity_id": f[1], "state": f[2] if not left else f[-1]}
            if f[1] in holds and positive:
                cond["for"] = {"minutes": holds[f[1]]}
            conditions.append(cond if positive else {"condition": "not", "conditions": [cond]})
        elif f[0] == "hour" and k + 1 < 24:
            conditions.append({"condition": "time", ("before" if left else "after"): f"{k + 1:02d}:00:00"})
            if not left and not triggers:
                triggers.append({"trigger": "time", "at": f"{k + 1:02d}:00:00"})
        elif f[0] == "weekend":
            conditions.append({"condition": "time", "weekday": WEEKDAYS if left else ["sat", "sun"]})
    if not path:
        triggers.append(
            {"trigger": "state", "entity_id": target, "to": p["other"], "for": {"minutes": p["hold_min"]}}
        )
    if not triggers:
        triggers.append({"trigger": "time_pattern", "minutes": "/5"})
    svc, data = service(target, state)
    act = {"action": svc, "target": {"entity_id": target}}
    if data:
        act["data"] = data
    name = p.get("name") or target
    if p["mode"] == "act":
        actions = [act]
    else:
        tag = "AGENT_HOUSE_" + "".join(ch if ch.isalnum() else "_" for ch in f"{target}_{state}").upper()
        button = {"tag": tag, "actions": [{"action": tag, "title": state}]}
        answer = {"trigger": "event", "event_type": ASK_EVENT, "event_data": {"action": tag}, "id": "yes"}
        by_hand = {"trigger": "state", "entity_id": target, "to": state, "id": "done"}
        actions = [
            {
                "action": NOTIFY,
                "data": {"title": "Agent House", "message": f"{name} → {state}?", "data": button},
            },
            # the owner answers, or does it themselves (then the question is moot), or nothing
            {
                "wait_for_trigger": [answer, by_hand],
                "timeout": {"minutes": ASK_TIMEOUT_MIN},
                "continue_on_timeout": True,
            },
            {"action": NOTIFY, "data": {"message": "clear_notification", "data": {"tag": tag}}},
            {
                "condition": "template",
                "value_template": "{{ wait.trigger is not none and wait.trigger.id == 'yes' }}",
            },
            act,
        ]
    return {
        "alias": f"{name} → {state}" + (" (asks first)" if p["mode"] == "ask" else ""),
        "description": f"Agent House (D35): when {describe(path, ha, p)}.",
        "triggers": triggers,
        "conditions": conditions,
        "actions": actions,
        "mode": "single",
    }
