# ruff: noqa: E501, B007, B905, UP017, B023  (throwaway prototype, D38 step 1)
"""PROTOTYPE — throwaway, not production. Lives on branch proto/predictive-habits.

Question: is "predict the owner's next manual action from the whole context, then act / ask / wait by
expected utility with a learnt cost of being wrong" better than today's habits.py idea ("a state that
recurs at about the same time becomes a time automation") — for different kinds of people, without
per-domain block lists?

Run:  .venv/bin/python -m sim.proto_predictive_habits [days] [seed]

The house here is NOT sim/household.py: there, behaviour is clock-driven by construction, so any
learner would learn the clock. Here a person has *wants* with causes (it is dark and I am in the living
room; I chose to watch TV; I go to bed) and acts on them with a human lag, forgets things, travels,
has guests; sensors pulse with presence and lie now and then. The wants are ground truth for scoring;
the learners only ever see the action log and sensor states, like in Home Assistant.

Scoring, per (entity, target state), over the days after a 14-day warm-up, retraining weekly on the
past only (prequential):
  act right  (the person wanted that state)            +1    (a switch they did not have to touch)
  act wrong  (they did not)                            -cost (hidden per-person cost, incl. the undo)
  ask right  (phone notification with a button)        +0.5
  ask wrong                                            -0.3  (an interruption)
"""

from __future__ import annotations

import math
import random
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
LAT, LON = 52.5, 13.4
START = date(2026, 9, 14)  # sunsets move ~19:20 -> ~15:55, and DST ends on Oct 25
LAMP, KITCHEN, HALL, BEDL, TV = "lamp", "kitchen", "hall", "bedroom", "tv"
ACTUATORS = [LAMP, KITCHEN, HALL, BEDL, TV]
SENSORS = ["person", "motion_living", "motion_kitchen", "motion_hall", "door"]
# hidden: what a wrong action costs THIS person (annoyance + the undo). The system never reads it,
# it only sees the cost after it acted wrong (in HA: how fast and how angrily it was undone).
TRUE_COST = {
    (LAMP, "on"): 1.5, (LAMP, "off"): 4, (KITCHEN, "on"): 1.5, (KITCHEN, "off"): 4,
    (HALL, "on"): 1, (HALL, "off"): 2, (BEDL, "on"): 3, (BEDL, "off"): 3,
    (TV, "on"): 4, (TV, "off"): 8,
}  # fmt: skip
FORGET = {LAMP: 0.08, KITCHEN: 0.05, HALL: 0.12, BEDL: 0.03, TV: 0.02}
TICK, HORIZON = 1, 3  # minutes: decide every minute, like a trigger; 'would do it within 3 min'
WARMUP, RETRAIN = 14, 7  # days
DEPTH, MIN_LEAF = 3, 30.0
COST_PRIOR, COST_PRIOR_N = 3.0, 2.0
ASK_GAIN, ASK_COST = 0.5, 0.3
SHADOW_PRIOR = 2.0  # the tree's own P counts as this many shadow ticks
SHADOW_MIN = 10  # a rule acts only after this many shadow ticks (the ladder: shadow -> ask -> act)
SUPPRESS = 60  # minutes after a wrong act or any ask


# --- the world ----------------------------------------------------------------------------------
def sun_elev(ts: float) -> float:
    d = datetime.fromtimestamp(ts, timezone.utc)
    n = d.timetuple().tm_yday
    decl = math.radians(23.44) * math.sin(2 * math.pi * (284 + n) / 365)
    ha = math.radians(15 * (d.hour + d.minute / 60 + LON / 15 - 12))
    lat = math.radians(LAT)
    return math.degrees(math.asin(math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha)))


def g(rng, sd):
    return rng.gauss(0, sd)


def day_plan(kind: str, d: int, day: date, rng: random.Random, trip: range) -> dict | None:
    """Minutes from local midnight. None = away all day."""
    if d in trip:
        return None
    weekend = day.weekday() >= 5
    guests = rng.random() < 0.07
    bed = 1350 + rng.lognormvariate(math.log(45), 0.6) - 15 + (60 if weekend else 0) + (90 if guests else 0)
    outings, meals = [], []
    if kind == "shift" and not weekend:
        if (d // 7) % 2 == 0:  # early shift
            wake = 285 + g(rng, 10)
            outings = [(wake + 45, 885 + g(rng, 20))]
            bed = 1290 + g(rng, 30)
        else:  # late shift
            wake = 540 + g(rng, 30)
            outings = [(795 + g(rng, 5), 1360 + g(rng, 15))]
            bed = 1360 + 70 + g(rng, 20)
    elif kind == "commuter" and not weekend and rng.random() > 0.04:
        wake = 405 + g(rng, 15)
        back = 1230 + g(rng, 30) if rng.random() < 0.15 else 1065 + g(rng, 50)
        outings = [(wake + 75 + g(rng, 8), back)]
    elif kind == "homebody":
        wake = 450 + rng.uniform(0, 180)
        bed = 1380 + rng.uniform(0, 150) + (90 if guests else 0)
        if rng.random() < 0.6:
            a = rng.uniform(600, 1080)
            outings = [(a, a + rng.uniform(40, 180))]
        meals = [(wake + 240, wake + 270)]
    else:  # weekend (commuter, shift) or a sick day
        wake = 525 + g(rng, 40)
        if rng.random() < 0.6:
            a = 780 + g(rng, 60)
            outings = [(a, max(a + 60, 1020 + g(rng, 90)))]
    home_evening = max([b for _, b in outings] + [wake + 60])
    din = max(home_evening + 15, 1110 + g(rng, 20))
    if din + 35 < bed - 30:
        meals.append((din, din + 35))
    tv = None
    start_lo = max(home_evening + 10, 1080)
    if rng.random() < (0.2 if guests else 0.55) and bed - 70 > start_lo:
        a = rng.uniform(start_lo, bed - 70)
        tv = (a, min(a + rng.uniform(40, 150), bed - rng.uniform(3, 12)))
    return {"wake": wake, "bed": bed, "read": rng.uniform(5, 35), "outings": outings, "meals": meals,
            "tv": tv, "guests": guests}  # fmt: skip


class World:
    """Minute arrays from START for `days` days: where the person is, darkness, wants, sensors."""

    def __init__(self, kind: str, days: int, seed: int):
        rng = random.Random(f"{kind}:{seed}")
        self.t0 = datetime(START.year, START.month, START.day, tzinfo=TZ).timestamp()
        self.n = days * 1440 + 60 * 3  # DST adds an hour; keep a small tail
        n = self.n
        self.loc = ["sleep"] * n
        self.tv = [0] * n
        trip_start = rng.randint(days // 2, days - 8)
        trip = range(trip_start, trip_start + 5)
        for d in range(days):
            day = START + timedelta(days=d)
            p = day_plan(kind, d, day, rng, trip)
            base = datetime(day.year, day.month, day.day, tzinfo=TZ)

            def idx(minutes: float) -> int:
                return int(((base + timedelta(minutes=minutes)).timestamp() - self.t0) // 60)

            if p is None:
                self.paint(idx(0), idx(1440), "out")
                continue
            w, b = p["wake"], p["bed"]
            self.paint(idx(w), idx(w + 5), "bed_awake")
            self.paint(idx(w + 5), idx(w + 35), "kitchen_bf")
            self.paint(idx(w + 35), idx(b - 2), "living")
            for a, e in p["meals"]:
                self.paint(idx(a), idx(e), "kitchen")
            for a, e in p["outings"]:
                self.paint(idx(a - 2), idx(a), "hall")
                self.paint(idx(a), idx(e), "out")
                self.paint(idx(e), idx(e + 3), "hall")
            self.paint(idx(b - 2), idx(b), "hall")
            self.paint(idx(b), idx(b + p["read"]), "bed_awake")
            if p["tv"]:
                for m in range(idx(p["tv"][0]), min(idx(p["tv"][1]), n)):
                    if self.loc[m] in ("living", "kitchen"):
                        self.tv[m] = 1
        self.elev = [sun_elev(self.t0 + 60 * m) for m in range(n)]
        dark = [e < 4 for e in self.elev]
        hour = [self.local(m).hour for m in range(n)]
        L = self.loc
        self.want = {
            LAMP: [int(dark[m] and L[m] in ("living", "kitchen", "hall") and (hour[m] >= 14 or hour[m] < 5))
                   for m in range(n)],
            KITCHEN: [int(dark[m] and L[m] in ("kitchen", "kitchen_bf")) for m in range(n)],
            HALL: [int(dark[m] and L[m] == "hall") for m in range(n)],
            BEDL: [int(dark[m] and L[m] == "bed_awake") for m in range(n)],
            TV: self.tv,
        }  # fmt: skip
        self.actions = self.act(rng)  # the manual log: (minute, entity, state)
        self.state = {e: self.timeline(e) for e in ACTUATORS}
        self.sensors(rng)

    def local(self, m: int) -> datetime:
        return datetime.fromtimestamp(self.t0 + 60 * m, TZ)

    def paint(self, a: int, b: int, loc: str) -> None:
        for m in range(max(a, 0), min(b, self.n)):
            self.loc[m] = loc

    def act(self, rng) -> list[tuple[int, str, str]]:
        """A person acts on a changed want with a lag; when the want follows them moving (or is a choice)
        the lag is short, when it only got dark around them they notice late. Sometimes they forget."""
        out = []
        for e in ACTUATORS:
            w, on = self.want[e], False
            for m in range(1, self.n):
                if w[m] == w[m - 1]:
                    continue
                moved = self.loc[m] != self.loc[m - 1] or e == TV
                if w[m] and not on:
                    t = m + (rng.uniform(0.2, 1.5) if moved else rng.uniform(2, 40))
                    if int(t) < self.n and w[min(int(t), self.n - 1)]:
                        out.append((int(t), e, "on"))
                        on = True
                elif not w[m] and on and rng.random() > FORGET[e]:
                    out.append((min(int(m + rng.uniform(0, 1)), self.n - 1), e, "off"))
                    on = False
        return sorted(out)

    def timeline(self, e: str) -> list[int]:
        s, cur, i = [0] * self.n, 0, 0
        acts = [(m, st) for m, ee, st in self.actions if ee == e]
        for m in range(self.n):
            while i < len(acts) and acts[i][0] <= m:
                cur = acts[i][1] == "on"
                i += 1
            s[m] = int(cur)
        return s

    def sensors(self, rng) -> None:
        n, L = self.n, self.loc
        self.sens = {"person": [int(L[m] != "out") for m in range(n)]}
        for name, places in (("motion_living", ("living",)), ("motion_kitchen", ("kitchen", "kitchen_bf")),
                             ("motion_hall", ("hall",))):  # fmt: skip
            arr, hold = [0] * n, -1  # a PIR: fires on entering and on movement, holds 2 min
            for m in range(n):
                there = L[m] in places
                if there and ((m and L[m - 1] not in places) or rng.random() < 0.35):
                    hold = m + 2
                arr[m] = int(m < hold)
            self.sens[name] = arr
        self.sens["door"] = [int(m > 0 and (L[m] == "out") != (L[m - 1] == "out")) for m in range(n)]
        for _ in range(int(0.3 * n / 1440)):  # ghost motion
            m, arr = rng.randrange(n - 2), self.sens[rng.choice(["motion_living", "motion_kitchen", "motion_hall"])]
            arr[m] = arr[m + 1] = 1
        for _ in range(int(0.1 * n / 1440)):  # the tracker flaps
            m = rng.randrange(n - 5)
            for k in range(m, m + 3):
                self.sens["person"][k] ^= 1


# --- features: the same recipe for every entity, nothing per domain ---------------------------------
SINCE_BINS = (2, 5, 15, 60, 180)


def since_bin(x: int) -> int:
    return next((i for i, b in enumerate(SINCE_BINS) if x < b), len(SINCE_BINS))


ELEV_BINS = (-6, -2, 2, 6, 15)


def features(w: World) -> tuple[list[str], list[list[int]], list[int]]:
    names, cols = [], []
    entities = [(e, w.state[e]) for e in ACTUATORS] + [(s, w.sens[s]) for s in SENSORS]
    for name, arr in entities:
        since, last = [0] * w.n, 0
        for m in range(w.n):
            if m and arr[m] != arr[m - 1]:
                last = m
            since[m] = since_bin(m - last)
        names += [f"{name}", f"{name}.since"]
        cols += [arr, since]
    elev = [next((i for i, b in enumerate(ELEV_BINS) if x < b), len(ELEV_BINS)) for x in w.elev]
    rising = [int(m + 10 < w.n and w.elev[m + 10] > w.elev[m]) for m in range(w.n)]
    ticks = list(range(0, w.n - HORIZON, TICK))
    hour = {m: w.local(m).hour for m in ticks}
    names += ["sun.elev", "sun.rising", "hour", "weekend"]
    X = [[c[m] for c in cols] + [elev[m], rising[m], hour[m], int(w.local(m).weekday() >= 5)] for m in ticks]
    return names, X, ticks


def pretty(names: list[str], f: int, k: int, left: bool) -> str:
    n = names[f]
    op = "≤" if left else ">"
    if n.endswith(".since"):
        return f"{n[:-6]} changed {'<' if left else '≥'} {SINCE_BINS[k]} min ago" if k < len(SINCE_BINS) \
            else f"{n[:-6]} unchanged {'≤' if left else '>'} 3 h"  # fmt: skip
    if n == "sun.elev":
        return f"sun {'<' if left else '≥'} {ELEV_BINS[k]}°"
    if n in ("hour",):
        return f"hour {op} {k}"
    return f"{n} {'off' if left else 'on'}" if k == 0 else f"{n} {op} {k}"


# --- a tiny weighted decision tree (pure Python: the add-on image has no numpy) ------------------------
def _h(p: float, t: float) -> float:
    if t <= 0 or p <= 0 or p >= t:
        return 0.0
    q = p / t
    return -(q * math.log(q) + (1 - q) * math.log(1 - q))


def grow(X, y, wt, idx, depth):
    pos = sum(wt[i] for i in idx if y[i])
    tot = sum(wt[i] for i in idx)
    node = {"p": (pos + 0.5) / (tot + 1), "n": tot, "pos": pos}
    if depth == 0 or pos == 0 or pos == tot:
        return node
    best, base = None, _h(pos, tot) * tot
    for f in range(len(X[0])):
        cnt = defaultdict(lambda: [0.0, 0.0])
        for i in idx:
            c = cnt[X[i][f]]
            c[0] += wt[i] * y[i]
            c[1] += wt[i]
        lp = lt = 0.0
        for k in sorted(cnt)[:-1]:
            lp += cnt[k][0]
            lt += cnt[k][1]
            if lt < MIN_LEAF or tot - lt < MIN_LEAF:
                continue
            gain = base - _h(lp, lt) * lt - _h(pos - lp, tot - lt) * (tot - lt)
            if best is None or gain > best[0]:
                best = (gain, f, k)
    if best is None or best[0] < 1e-3 * tot:
        return node
    _, f, k = best
    node.update(f=f, k=k,
                l=grow(X, y, wt, [i for i in idx if X[i][f] <= k], depth - 1),
                r=grow(X, y, wt, [i for i in idx if X[i][f] > k], depth - 1))  # fmt: skip
    return node


def predict(node, x) -> tuple[float, tuple]:
    """(the tree's P, the leaf's rule as a key that survives a retrain when the split is the same)"""
    path = []
    while "f" in node:
        left = x[node["f"]] <= node["k"]
        path.append((node["f"], node["k"], left))
        node = node["l"] if left else node["r"]
    return node["p"], tuple(path)


def rules(node, names, path=(), p_min=0.5):
    if "f" not in node:
        return [(node["p"], node["pos"], path)] if node["p"] >= p_min else []
    f, k = node["f"], node["k"]
    return rules(node["l"], names, path + (pretty(names, f, k, True),), p_min) + \
        rules(node["r"], names, path + (pretty(names, f, k, False),), p_min)  # fmt: skip


# --- the two policies -------------------------------------------------------------------------------
def labels(w: World, ticks: list[int], e: str, s: str) -> tuple[list[bool], list[int]]:
    """opportunity (the device is not in that state) and label (the person did it within HORIZON)."""
    target = int(s == "on")
    times = sorted(m for m, ee, st in w.actions if ee == e and st == s)
    opp, lab, j = [], [], 0
    for m in ticks:
        while j < len(times) and times[j] <= m:
            j += 1
        opp.append(w.state[e][m] != target)
        lab.append(int(j < len(times) and times[j] <= m + HORIZON))
    return opp, lab


def habits_rule(w: World, e: str, s: str, upto_day: int) -> list[tuple[str, float]]:
    """Today's idea in short (habits.py D31 gate + the time automation the cards turn into): per
    workdays / weekends, episodes on time within ±45 min of the median on ≥ 50 % of the eligible days,
    ≥ 3 days, explaining ≥ 60 %. Generous: the automation also gets `someone home`."""
    eps = []
    for m, ee, st in w.actions:
        if ee == e and st == s and m < upto_day * 1440:
            t = w.local(m)
            minute = t.hour * 60 + t.minute
            minute += 1440 if minute < 240 else 0
            day = (t - timedelta(hours=4)).date()
            eps.append((day, minute))
    out = []
    for group, is_group in (("workdays", lambda d: d.weekday() < 5), ("weekends", lambda d: d.weekday() >= 5)):
        g_eps = [(d, mi) for d, mi in eps if is_group(d)]
        if len({d for d, _ in g_eps}) < 3:
            continue
        med = sorted(mi for _, mi in g_eps)[len(g_eps) // 2]
        on_time = [(d, mi) for d, mi in g_eps if abs(mi - med) <= 45]
        first = min(d for d, _ in g_eps)
        last = START + timedelta(days=upto_day - 1)
        eligible = sum(1 for k in range((last - first).days + 1) if is_group(first + timedelta(k)))
        if len({d for d, _ in on_time}) >= max(3, 0.5 * eligible) and len(on_time) >= 0.6 * len(g_eps):
            out.append((group, med))
    return out


def evaluate(kind: str, days: int, seed: int) -> dict:
    w = World(kind, days, seed)
    names, X, ticks = features(w)
    day_of = [(m // 1440) for m in ticks]
    res = {"kind": kind, "manual": sum(1 for m, _, _ in w.actions if m >= WARMUP * 1440)}
    per = {}
    for e in ACTUATORS:
        for s in ("on", "off"):
            opp, lab = labels(w, ticks, e, s)
            target, cost = int(s == "on"), TRUE_COST[(e, s)]
            stats = {k: 0.0 for k in ("new_ok", "new_bad", "ask_ok", "ask_bad", "new_u", "old_ok", "old_bad", "old_u")}
            c_obs: list[float] = []
            tree, auto, rng = None, [], random.Random(f"{e}{s}{seed}")
            skip_new = skip_old = ask_quiet = -1
            shadow: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0.0])  # rule -> [right, seen]
            for i, m in enumerate(ticks):
                d = day_of[i]
                if d < WARMUP:
                    continue
                if (d - WARMUP) % RETRAIN == 0 and (tree is None or ticks[i - 1] // 1440 != d):
                    tr = [j for j in range(i) if opp[j] and (lab[j] or rng.random() < 0.05)]
                    wt = [1.0 if lab[j] else 20.0 for j in range(len(ticks))]
                    tree = grow(X, lab, wt, tr, DEPTH)
                    auto = habits_rule(w, e, s, d)
                if not opp[i]:
                    continue
                wanted = w.want[e][m] == target or bool(lab[i])  # wanted now, or they were about to
                # the new policy
                p_tree, leaf = predict(tree, X[i])
                right, seen = shadow[leaf]
                p = (right + SHADOW_PRIOR * p_tree) / (seen + SHADOW_PRIOR) if USE_SHADOW else p_tree
                shadow[leaf][0] += lab[i]  # what HA can see: did the person do it within 3 min
                shadow[leaf][1] += 1
                if m >= skip_new:
                    c_hat = (COST_PRIOR * COST_PRIOR_N + sum(c_obs)) / (COST_PRIOR_N + len(c_obs))
                    proven = seen >= SHADOW_MIN or not USE_SHADOW
                    if proven and p * 1 - (1 - p) * c_hat > 0:
                        if wanted:
                            stats["new_ok"] += 1
                            stats["new_u"] += 1
                            skip_new = next_change(w, e, m)
                        else:
                            stats["new_bad"] += 1
                            stats["new_u"] -= cost
                            c_obs.append(cost)
                            skip_new = m + SUPPRESS
                    elif p * ASK_GAIN - (1 - p) * ASK_COST > 0 and m >= ask_quiet:
                        stats["ask_ok" if wanted else "ask_bad"] += 1
                        stats["new_u"] += ASK_GAIN if wanted else -ASK_COST
                        ask_quiet = m + SUPPRESS
                        if wanted:
                            skip_new = next_change(w, e, m)
                # today's idea: a time automation
                if m >= skip_old and w.sens["person"][m]:
                    t = w.local(m)
                    minute = t.hour * 60 + t.minute + (1440 if t.hour < 4 else 0)
                    wk = (t - timedelta(hours=4)).weekday() >= 5
                    if any((grp == "weekends") == wk and 0 <= minute - at < TICK for grp, at in auto):
                        if wanted:
                            stats["old_ok"] += 1
                            stats["old_u"] += 1
                            skip_old = next_change(w, e, m)
                        else:
                            stats["old_bad"] += 1
                            stats["old_u"] -= cost
                            skip_old = m + SUPPRESS
            stats["c_hat"] = (COST_PRIOR * COST_PRIOR_N + sum(c_obs)) / (COST_PRIOR_N + len(c_obs))
            stats["rules"] = rules(tree, names, p_min=stats["c_hat"] / (1 + stats["c_hat"]))
            stats["auto"] = auto
            per[(e, s)] = stats
    res["per"] = per
    return res


def next_change(w: World, e: str, m: int) -> int:
    cur = w.state[e][m]
    for k in range(m + 1, w.n):
        if w.state[e][k] != cur:
            return k + 1
    return w.n


def hhmm(minute: float) -> str:
    minute = int(minute) % 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


USE_SHADOW = True


def main() -> None:
    global USE_SHADOW
    if "--no-shadow" in sys.argv:
        USE_SHADOW = False
        sys.argv.remove("--no-shadow")
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 84
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    print(f"PROTOTYPE · {days} days from {START} (Berlin), warm-up {WARMUP}, weekly retrain, seed {seed}\n")
    for kind in ("commuter", "shift", "homebody"):
        r = evaluate(kind, days, seed)
        tot = defaultdict(float)
        for st in r["per"].values():
            for k, v in st.items():
                if isinstance(v, float):
                    tot[k] += v
        print(f"== {kind}: {r['manual']} manual actions after warm-up")
        print(f"   today (time automations): right {tot['old_ok']:.0f} · wrong {tot['old_bad']:.0f} · utility {tot['old_u']:+.1f}")
        print(f"   new (predict + act/ask):  act right {tot['new_ok']:.0f} · wrong {tot['new_bad']:.0f} · "
              f"ask right {tot['ask_ok']:.0f} · wrong {tot['ask_bad']:.0f} · utility {tot['new_u']:+.1f}")  # fmt: skip
        print("   per device       | today: rule → right/wrong          | new: act r/w · ask r/w · learnt cost")
        for (e, s), st in r["per"].items():
            auto = ", ".join(f"{g[:2]} {hhmm(a)}" for g, a in st["auto"]) or "—"
            print(f"   {e:8} → {s:3}   | {auto:18} {st['old_ok']:3.0f}/{st['old_bad']:<3.0f}"
                  f"        | {st['new_ok']:3.0f}/{st['new_bad']:<3.0f} · {st['ask_ok']:3.0f}/{st['ask_bad']:<3.0f}"
                  f" · {st['c_hat']:.1f}")  # fmt: skip
        if kind == "commuter":
            print("   what the new one learnt (last model, leaves above its act threshold):")
            for (e, s), st in r["per"].items():
                for p, pos, path in st["rules"][:2]:
                    print(f"     {e} → {s}: P={p:.2f} when " + " ∧ ".join(path))
        print()


if __name__ == "__main__":
    main()
