# ruff: noqa: E501, B007, B905  (throwaway prototype, D38 step 1)
"""PROTOTYPE — throwaway, not production. Lives on branch proto/learner (step 1 of D38 in docs/PLAN.md).

Questions:
  E1  Does a gradient-boosted model (what the Jetson would run) beat today's shallow tree (D35,
      `core/predict.py`) at predicting the owner's next manual action, scored by the same utility —
      for single people and couples, with PIR motion or with mmWave radars (the owner's house)?
      And can the booster be distilled back into a depth-3 tree whose leaves are HA rules, without
      losing what it learnt?
  E2  Can a template's parameter (the "room light controller" off-timeout) be learnt from the owner's
      corrections (light switched off while they sat still → they switch it on again)?

Run:  .venv/proto/bin/python -m sim.proto_learner [days] [seeds]
      (.venv/proto = python3 -m venv + pip install numpy scikit-learn; not the add-on's venv)

The booster is scikit-learn's HistGradientBoostingClassifier: the same algorithm family as LightGBM
(histogram GBDT, leaf-wise), chosen here only because it needs no system libomp on macOS. The D35 tree
is sklearn's DecisionTreeClassifier with the same settings (entropy, depth 3, threshold splits on the
same binned features), so all variants run at numpy speed.

The world, the wants, the costs and the scoring come from sim/proto_predictive_habits.py (D35's
prototype): a person acts on *wants* with causes and a human lag; the learners only see the action log
and the sensors, like in Home Assistant. New here: several people in one home (one `anyone_home`
sensor, no phone tracking — the owner's setup), a bed sensor, mmWave radars with dropouts while a
person sits still, sequence features for the booster, distillation.
"""

from __future__ import annotations

import os

os.environ.setdefault("OMP_NUM_THREADS", "1")  # small data: threads cost more than they give

import math
import random
import sys
import time
import zlib
from collections import defaultdict
from datetime import datetime, timedelta
from multiprocessing import Pool

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor

from sim.proto_predictive_habits import (
    ACTUATORS,
    ASK_COST,
    ASK_GAIN,
    BEDL,
    COST_PRIOR,
    COST_PRIOR_N,
    ELEV_BINS,
    FORGET,
    HALL,
    HORIZON,
    KITCHEN,
    LAMP,
    RETRAIN,
    SHADOW_MIN,
    SHADOW_PRIOR,
    SINCE_BINS,
    START,
    SUPPRESS,
    TRUE_COST,
    TV,
    TZ,
    WARMUP,
    day_plan,
    sun_elev,
)

PLACES = ["sleep", "bed_awake", "kitchen_bf", "living", "kitchen", "hall", "out"]
P = {name: i for i, name in enumerate(PLACES)}
SENSORS = ["anyone_home", "motion_living", "motion_kitchen", "motion_hall", "motion_bedroom", "bed", "door"]
ROOMS = {
    "motion_living": ("living",),
    "motion_kitchen": ("kitchen", "kitchen_bf"),
    "motion_hall": ("hall",),
    "motion_bedroom": ("bed_awake",),  # a PIR does not see a sleeper; a radar does (added below)
}
HOUSEHOLDS = {
    "commuter": ("commuter",),
    "shift": ("shift",),
    "homebody": ("homebody",),
    "couple": ("commuter", "homebody"),
    "shift+commuter": ("shift", "commuter"),
}


# --- the world: N people, one house ------------------------------------------------------------------
class House:
    def __init__(self, kinds: tuple[str, ...], days: int, seed: int, sensor: str):
        rng = random.Random(f"{'+'.join(kinds)}:{seed}:{sensor}")
        self.t0 = datetime(START.year, START.month, START.day, tzinfo=TZ).timestamp()
        self.n = n = days * 1440 + 180
        self.days = days
        self.locs, tvs = [], []
        for k, kind in enumerate(kinds):
            loc, tv = self.person(kind, days, random.Random(f"{kind}:{seed}:{k}"))
            self.locs.append(loc)
            tvs.append(tv)
        self.elev = np.array([sun_elev(self.t0 + 60 * m) for m in range(n)])
        local = [datetime.fromtimestamp(self.t0 + 60 * m, TZ) for m in range(n)]
        self.hour = np.array([t.hour for t in local])
        self.weekend = np.array([t.weekday() >= 5 for t in local], dtype=int)
        dark = self.elev < 4
        L = np.stack(self.locs)  # people × minutes

        def anyone(*places: str) -> np.ndarray:
            return np.isin(L, [P[p] for p in places]).any(axis=0)

        self.anyone = anyone
        self.want = {
            LAMP: (dark & anyone("living", "kitchen", "hall") & ((self.hour >= 14) | (self.hour < 5))).astype(
                int
            ),
            KITCHEN: (dark & anyone("kitchen", "kitchen_bf")).astype(int),
            HALL: (dark & anyone("hall")).astype(int),
            BEDL: (dark & anyone("bed_awake")).astype(int),
            TV: np.stack(tvs).any(axis=0).astype(int),
        }
        moved = np.zeros(n, dtype=bool)
        moved[1:] = (L[:, 1:] != L[:, :-1]).any(axis=0)
        self.actions = self.act(rng, moved)
        self.state = {e: self.timeline(e) for e in ACTUATORS}
        self.sens = self.sensors(rng, sensor)

    def person(self, kind: str, days: int, rng: random.Random) -> tuple[np.ndarray, np.ndarray]:
        n = self.n
        loc = np.full(n, P["sleep"], dtype=np.int8)
        tv = np.zeros(n, dtype=np.int8)
        trip_start = rng.randint(days // 2, days - 8)
        trip = range(trip_start, trip_start + 5)

        def paint(a: int, b: int, place: str) -> None:
            loc[max(a, 0) : min(b, n)] = P[place]

        for d in range(days):
            day = START + timedelta(days=d)
            p = day_plan(kind, d, day, rng, trip)
            base = datetime(day.year, day.month, day.day, tzinfo=TZ)

            def idx(minutes: float, base=base) -> int:
                return int(((base + timedelta(minutes=minutes)).timestamp() - self.t0) // 60)

            if p is None:
                paint(idx(0), idx(1440), "out")
                continue
            w, b = p["wake"], p["bed"]
            paint(idx(w), idx(w + 5), "bed_awake")
            paint(idx(w + 5), idx(w + 35), "kitchen_bf")
            paint(idx(w + 35), idx(b - 2), "living")
            for a, e in p["meals"]:
                paint(idx(a), idx(e), "kitchen")
            for a, e in p["outings"]:
                paint(idx(a - 2), idx(a), "hall")
                paint(idx(a), idx(e), "out")
                paint(idx(e), idx(e + 3), "hall")
            paint(idx(b - 2), idx(b), "hall")
            paint(idx(b), idx(b + p["read"]), "bed_awake")
            if p["tv"]:
                a, e = idx(p["tv"][0]), min(idx(p["tv"][1]), n)
                seg = loc[a:e]
                tv[a:e] = np.isin(seg, [P["living"], P["kitchen"]])
        return loc, tv

    def act(self, rng: random.Random, moved: np.ndarray) -> list[tuple[int, str, str]]:
        """As in D35's prototype: a lag when the want follows someone moving, a long one when it only
        got dark around them; sometimes they forget to switch off. Who acts is not recorded."""
        out = []
        for e in ACTUATORS:
            w, on = self.want[e], False
            for m in np.flatnonzero(np.diff(w)) + 1:
                quick = moved[m] or e == TV
                if w[m] and not on:
                    t = m + (rng.uniform(0.2, 1.5) if quick else rng.uniform(2, 40))
                    if int(t) < self.n and w[min(int(t), self.n - 1)]:
                        out.append((int(t), e, "on"))
                        on = True
                elif not w[m] and on and rng.random() > FORGET[e]:
                    out.append((min(int(m + rng.uniform(0, 1)), self.n - 1), e, "off"))
                    on = False
        return sorted(out)

    def timeline(self, e: str) -> np.ndarray:
        s = np.zeros(self.n, dtype=np.int8)
        cur, last = 0, 0
        for m, ee, st in self.actions:
            if ee == e:
                s[last:m] = cur
                cur, last = int(st == "on"), m
        s[last:] = cur
        return s

    def sensors(self, rng: random.Random, kind: str) -> dict[str, np.ndarray]:
        n = self.n
        L = np.stack(self.locs)
        sens = {"anyone_home": (L != P["out"]).any(axis=0).astype(np.int8)}
        for name, places in ROOMS.items():
            if kind == "radar" and name == "motion_bedroom":
                places = places + ("sleep",)
            arr = np.zeros(n, dtype=np.int8)
            for loc in self.locs:
                there = np.isin(loc, [P[p] for p in places])
                if kind == "pir":  # fires on entering and on movement, holds 2 min
                    hold = -1
                    for m in np.flatnonzero(there):
                        if (m and not there[m - 1]) or rng.random() < 0.35:
                            hold = m + 2
                        if m < hold:
                            arr[m:hold] = 1
                else:  # mmWave radar: holds while someone is there, drops out when they sit still
                    r = there.astype(np.int8)
                    for m in np.flatnonzero(there):
                        if rng.random() < 0.03:
                            k = max(1, round(rng.lognormvariate(math.log(1.2), 0.9)))
                            r[m : m + k] = 0
                    r &= there  # a dropout never outlasts the stay
                    arr |= r
            sens[name] = arr
        for _ in range(int(0.3 * n / 1440)):  # ghosts
            m = rng.randrange(n - 2)
            sens[rng.choice(list(ROOMS))][m : m + 2] = 1
        sens["bed"] = np.isin(L, [P["sleep"], P["bed_awake"]]).any(axis=0).astype(np.int8)
        out = L == P["out"]
        door = np.zeros(n, dtype=np.int8)
        door[1:] = (out[:, 1:] != out[:, :-1]).any(axis=0)
        sens["door"] = door
        return sens


# --- features -----------------------------------------------------------------------------------------
def since(arr: np.ndarray) -> np.ndarray:
    """Minutes since the array last changed."""
    n = len(arr)
    ch = np.zeros(n, dtype=bool)
    ch[1:] = arr[1:] != arr[:-1]
    last = np.maximum.accumulate(np.where(ch, np.arange(n), 0))
    return np.arange(n) - last


def features(h: House) -> tuple[list[str], np.ndarray, list[str], np.ndarray]:
    """Base = what D35 uses and what maps onto HA triggers/conditions (state, changed < N min ago,
    sun, hour, weekend). Seq = extra context only a model can use (counts and order of recent events)."""
    entities = [(e, h.state[e]) for e in ACTUATORS] + [(s, h.sens[s]) for s in SENSORS]
    names, cols = [], []
    sinces, changes = [], []
    for name, arr in entities:
        s = since(arr)
        sinces.append(s)
        names += [name, f"{name}.since"]
        cols += [arr, np.digitize(s, SINCE_BINS, right=False)]
        ch = np.zeros(h.n, dtype=np.int32)
        ch[1:] = arr[1:] != arr[:-1]
        changes.append(ch)
    names += ["sun.elev", "sun.rising", "hour", "weekend"]
    rising = np.zeros(h.n, dtype=int)
    rising[:-10] = h.elev[10:] > h.elev[:-10]
    cols += [np.digitize(h.elev, ELEV_BINS), rising, h.hour, h.weekend]
    base = np.stack(cols, axis=1).astype(np.float32)

    seq_names, seq = [], []
    C = np.stack(changes)  # entity × minute
    cum = np.cumsum(C, axis=1)
    for k, (name, _) in enumerate(entities):  # changes in the last 10 and 30 minutes
        for win in (10, 30):
            c = cum[k].copy()
            c[win:] -= cum[k][:-win]
            seq.append(c)
            seq_names.append(f"{name}.changes{win}")
    S = np.stack(sinces)
    for name, s in zip([e for e, _ in entities], S, strict=False):  # exact minutes since, capped
        seq.append(np.minimum(s, 600))
        seq_names.append(f"{name}.minutes")
    order = np.argsort(S, axis=0)  # the last three entities that changed, as categories
    for r in range(3):
        seq.append(order[r])
        seq_names.append(f"last{r + 1}")
    rooms = [SENSORS.index(r) + len(ACTUATORS) for r in ROOMS]
    seq.append((S[rooms] < 5).sum(axis=0))
    seq_names.append("rooms_active5")
    seq += [h.elev, (h.hour * 60 + np.arange(h.n) % 60)]
    seq_names += ["sun.deg", "minute_of_day"]
    full = np.concatenate([base, np.stack(seq, axis=1).astype(np.float32)], axis=1)
    return names, base, names + seq_names, full


# --- learners -----------------------------------------------------------------------------------------
P_BINS = np.array([0.02, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9])


def leaf_paths(tree) -> dict[int, tuple]:
    """leaf node id → its path (feature, threshold, left) — survives a retrain when the splits repeat."""
    t, out = tree.tree_, {}

    def walk(node: int, path: tuple) -> None:
        if t.children_left[node] == -1:
            out[node] = path
            return
        f, k = int(t.feature[node]), round(float(t.threshold[node]), 2)
        walk(t.children_left[node], path + ((f, k, True),))
        walk(t.children_right[node], path + ((f, k, False),))

    walk(0, ())
    return out


def fit_tree(X, y, w):
    t = DecisionTreeClassifier(
        criterion="entropy", max_depth=3, min_samples_leaf=4, min_impurity_decrease=1e-4
    )
    t.fit(X, y, sample_weight=w)
    return t


def tree_predict(t, X) -> tuple[np.ndarray, list]:
    proba = t.predict_proba(X)
    p = proba[:, list(t.classes_).index(1)] if 1 in t.classes_ else np.zeros(len(X))
    paths = leaf_paths(t)
    return p, [paths[i] for i in t.apply(X)]


def fit_gbdt(X, y, w, cat):
    g = HistGradientBoostingClassifier(
        max_iter=100, learning_rate=0.08, max_leaf_nodes=15, min_samples_leaf=20, l2_regularization=1.0,
        categorical_features=cat, early_stopping=False, random_state=0,
    )  # fmt: skip
    g.fit(X, y, sample_weight=w)
    return g


def gbdt_p(g, X) -> np.ndarray:
    return g.predict_proba(X)[:, 1] if len(g.classes_) == 2 else np.zeros(len(X))


def train(variant, base, full, cat, y, w, tr, days_tr):
    """→ predict(rows) giving (P, rule key per row)."""
    if variant == "tree" or y[tr].sum() == 0:
        t = fit_tree(base[tr], y[tr], w[tr])
        return lambda rows: tree_predict(t, base[rows])
    if variant == "gbdt":
        g = fit_gbdt(full[tr], y[tr], w[tr], cat)
        return lambda rows: (lambda p: (p, [("bin", int(b)) for b in np.digitize(p, P_BINS)]))(
            gbdt_p(g, full[rows])
        )
    if variant == "gbdt-base":
        g = fit_gbdt(base[tr], y[tr], w[tr], None)
        return lambda rows: (lambda p: (p, [("bin", int(b)) for b in np.digitize(p, P_BINS)]))(
            gbdt_p(g, base[rows])
        )
    # distilled: the booster (full features) teaches a depth-3 tree on base features. The teacher's
    # P on the training rows is out-of-fold (3 folds by day), so the student does not copy overfit.
    soft = np.zeros(len(tr))
    folds = days_tr % 3
    for f in range(3):
        fit_rows, pred_rows = tr[folds != f], tr[folds == f]
        if y[fit_rows].sum() == 0 or len(pred_rows) == 0:
            continue
        soft[folds == f] = gbdt_p(fit_gbdt(full[fit_rows], y[fit_rows], w[fit_rows], cat), full[pred_rows])
    s = DecisionTreeRegressor(max_depth=3, min_samples_leaf=4, min_impurity_decrease=1e-6)
    s.fit(base[tr], soft, sample_weight=w[tr])
    paths = leaf_paths(s)
    return lambda rows: (np.clip(s.predict(base[rows]), 0, 1), [paths[i] for i in s.apply(base[rows])])


# --- the policy (identical for every variant: D35's shadow ladder + expected utility) ---------------
def labels(h: House, e: str, s: str) -> tuple[np.ndarray, np.ndarray]:
    target = int(s == "on")
    times = np.array(sorted(m for m, ee, st in h.actions if ee == e and st == s), dtype=np.int64)
    m = np.arange(h.n)
    j = np.searchsorted(times, m, side="right")
    lab = np.zeros(h.n, dtype=np.int8)
    ok = j < len(times)
    lab[ok] = times[j[ok]] <= m[ok] + HORIZON
    return h.state[e] != target, lab


def next_change(arr: np.ndarray) -> np.ndarray:
    n = len(arr)
    nxt = np.full(n, n, dtype=np.int64)
    ch = np.flatnonzero(arr[1:] != arr[:-1]) + 1
    k = np.searchsorted(ch, np.arange(n), side="right")
    ok = k < len(ch)
    nxt[ok] = ch[k[ok]] + 1
    return nxt


VARIANTS = ("tree", "gbdt-base", "gbdt", "distilled")


def evaluate(h: House, seed: int) -> dict:
    names, base, fnames, full = features(h)
    cat = np.array([n.startswith("last") for n in fnames])
    n_ticks = h.n - HORIZON
    day = np.arange(h.n) // 1440
    out = {v: defaultdict(float) for v in VARIANTS}
    for e in ACTUATORS:
        nxt = next_change(h.state[e])
        for s in ("on", "off"):
            opp, lab = labels(h, e, s)
            target, cost = int(s == "on"), TRUE_COST[(e, s)]
            wanted = (h.want[e] == target) | (lab == 1)
            rng = np.random.default_rng(zlib.crc32(f"{e}{s}{seed}".encode()))
            keep = (lab == 1) | (rng.random(h.n) < 0.05)
            w = np.where(lab == 1, 1.0, 20.0)
            for v in VARIANTS:
                st = out[v]
                shadow: dict = defaultdict(lambda: [0.0, 0.0])
                c_obs: list[float] = []
                skip, ask_quiet = -1, -1
                for d0 in range(WARMUP, h.days, RETRAIN):
                    tr = np.flatnonzero(opp[: d0 * 1440] & keep[: d0 * 1440])
                    rows = np.arange(d0 * 1440, min((d0 + RETRAIN) * 1440, n_ticks))
                    rows = rows[opp[rows]]
                    if len(rows) == 0:
                        continue
                    pred = train(v, base, full, cat, lab, w, tr, day[tr])
                    ps, keys = pred(rows)
                    for m, p_model, key in zip(rows, ps, keys, strict=False):
                        right, seen = shadow[key]
                        p = (right + SHADOW_PRIOR * p_model) / (seen + SHADOW_PRIOR)
                        shadow[key][0] += lab[m]
                        shadow[key][1] += 1
                        if m < skip:
                            continue
                        c_hat = (COST_PRIOR * COST_PRIOR_N + sum(c_obs)) / (COST_PRIOR_N + len(c_obs))
                        if seen >= SHADOW_MIN and p - (1 - p) * c_hat > 0:
                            if wanted[m]:
                                st["act_ok"] += 1
                                st["u"] += 1
                                skip = nxt[m]
                            else:
                                st["act_bad"] += 1
                                st["u"] -= cost
                                c_obs.append(cost)
                                skip = m + SUPPRESS
                        elif p * ASK_GAIN - (1 - p) * ASK_COST > 0 and m >= ask_quiet:
                            st["ask_ok" if wanted[m] else "ask_bad"] += 1
                            st["u"] += ASK_GAIN if wanted[m] else -ASK_COST
                            ask_quiet = m + SUPPRESS
                            if wanted[m]:
                                skip = nxt[m]
    eval_days = h.days - WARMUP
    for v in VARIANTS:
        out[v]["per_day_asks"] = (out[v]["ask_ok"] + out[v]["ask_bad"]) / eval_days
    out["manual"] = sum(1 for m, _, _ in h.actions if m >= WARMUP * 1440) / eval_days
    return out


# --- E2: the room light controller's off-timeout, learnt from corrections ----------------------------
C_OFF = 4.0  # a light switched off on someone sitting there (the D35 lamp "off" cost)
E_MIN = 0.05  # a minute of light in an empty room (20 min = one unit); the owner's energy preference
TRAIN_DAYS = 28
T_GRID = range(1, 31)


def controller_runs(h: House) -> list[tuple[int, int, bool]]:
    """Radar-off runs in the living room in the dark: (start, length, someone actually still there)."""
    radar = h.sens["motion_living"].astype(bool)
    there = h.anyone("living")
    dark = h.elev < 4
    runs, m, n = [], 1, h.n
    while m < n:
        if radar[m - 1] and not radar[m] and dark[m]:
            k = m
            while k < n and not radar[k]:
                k += 1
            runs.append((m, k - m, bool(there[m])))
            m = k
        m += 1
    return runs


def cost(runs, T: int) -> float:
    c = 0.0
    for _, length, still in runs:
        if still:
            c += C_OFF if length >= T else 0.0
        else:
            c += E_MIN * min(T, length)
    return c


def learn_timeout(h: House, runs, T0: int, rng: random.Random) -> tuple[int, float]:
    """What HA shows with the controller deployed at T0, on the training days only:
    - a run ≥ T0 switched the light off; a manual "on" within 2 min after it (90 % of the time, if
      someone was there) marks it a dropout, no correction marks it a real leave;
    - a run < T0 switched nothing: classified by context — someone *appearing* in another room (a
      radar switching on) or the door within the run's first 3 minutes means they walked out, else a
      dropout. (Another room merely being occupied says nothing when two people live there.)
    → the T that minimises the same cost over these labelled runs, and the corrections per day."""
    other = np.zeros(h.n, dtype=bool)
    for s in ("motion_kitchen", "motion_hall", "motion_bedroom", "door"):
        a = h.sens[s].astype(bool)
        other[1:] |= a[1:] & ~a[:-1]
    labelled, corrections = [], 0
    for start, length, still in runs:
        if start >= TRAIN_DAYS * 1440:
            break
        if length >= T0:
            corrected = still and rng.random() < 0.9
            corrections += corrected
            labelled.append((start, length, corrected))
        else:
            labelled.append((start, length, not other[start - 1 : start + min(length, 3)].any()))
    best = min(T_GRID, key=lambda T: (cost(labelled, T), T))
    # the same choice with the dropout tail smoothed: a log-normal fitted to the dropout lengths
    # (a month has only a handful of long dropouts; counting them alone under-estimates the tail)
    drops = [math.log(length) for _, length, d in labelled if d]
    leaves = [length for _, length, d in labelled if not d]
    mu = sum(drops) / max(len(drops), 1)
    sd = max(math.sqrt(sum((x - mu) ** 2 for x in drops) / max(len(drops) - 1, 1)), 0.3)

    def smooth(T: int) -> float:
        tail = 0.5 * math.erfc((math.log(max(T - 0.5, 0.5)) - mu) / (sd * math.sqrt(2)))
        return C_OFF * len(drops) * tail + E_MIN * sum(min(T, x) for x in leaves)

    fit = min(T_GRID, key=lambda T: (smooth(T), T))
    return best, fit, corrections / TRAIN_DAYS


def e2(h: House, seed: int) -> dict:
    runs = controller_runs(h)
    test = [r for r in runs if r[0] >= TRAIN_DAYS * 1440]
    test_days = h.days - TRAIN_DAYS
    t_star = min(T_GRID, key=lambda T: (cost(test, T), T))
    res = {"T*": t_star, "cost*": cost(test, t_star) / test_days}
    for T0 in (2, 15):
        t_hat, t_fit, corr = learn_timeout(h, runs, T0, random.Random(seed))
        res[T0] = {"T": t_hat, "Tfit": t_fit, "corr/day": corr, "cost_T0": cost(test, T0) / test_days,
                   "cost_hat": cost(test, t_hat) / test_days, "cost_fit": cost(test, t_fit) / test_days}  # fmt: skip
    return res


# --- report -------------------------------------------------------------------------------------------
def run_one(job: tuple) -> tuple:
    sensor, hh, seed, days = job
    h = House(HOUSEHOLDS[hh], days, seed, sensor)
    return sensor, hh, seed, evaluate(h, seed), (e2(h, seed) if sensor == "radar" else None)


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 84
    seeds = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    print(f"PROTOTYPE · {days} days from {START}, warm-up {WARMUP}, weekly retrain, {seeds} seeds\n")
    t_start = time.time()
    jobs = [
        (sensor, hh, seed, days)
        for sensor in ("pir", "radar")
        for hh in HOUSEHOLDS
        for seed in range(1, seeds + 1)
    ]
    with Pool(os.cpu_count()) as pool:
        results = pool.map(run_one, jobs, chunksize=1)
    print("E1 · utility over the days after warm-up (act +1 / −cost, ask +0.5 / −0.3), sum of seeds")
    print(f"   {'household':16} {'sensor':6} {'manual/d':>8} | " + " | ".join(f"{v:>22}" for v in VARIANTS))
    print(f"   {'':16} {'':6} {'':>8} | " + " | ".join(f"{'utility  act r/w  ask/d':>22}" for _ in VARIANTS))
    e2_rows = []
    for sensor in ("pir", "radar"):
        for hh in HOUSEHOLDS:
            agg = {v: defaultdict(float) for v in VARIANTS}
            manual = 0.0
            for s_, h_, seed, r, r2 in results:
                if (s_, h_) != (sensor, hh):
                    continue
                manual += r["manual"] / seeds
                for v in VARIANTS:
                    for k, x in r[v].items():
                        agg[v][k] += x
                if r2:
                    e2_rows.append((hh, seed, r2))
            cells = []
            for v in VARIANTS:
                a = agg[v]
                cells.append(
                    f"{a['u']:+8.0f} {a['act_ok']:4.0f}/{a['act_bad']:<4.0f} {a['per_day_asks'] / seeds:4.1f}"
                )
            print(f"   {hh:16} {sensor:6} {manual:8.1f} | " + " | ".join(f"{c:>22}" for c in cells))
    print(f"   ({time.time() - t_start:.0f} s on {os.cpu_count()} cores)\n")

    print("E2 · living-room light controller (radar), off-timeout learnt on days 1–28, scored on the rest")
    print(f"   cost/day = {C_OFF:g} per light switched off on someone + {E_MIN:g} per empty lit minute")
    print("   T* = best timeout in hindsight on the scored days; T̂ = counted, T̃ = tail smoothed (learnt)")
    print(
        f"   {'household':16} seed | T* cost | T0=2: corr/d cost | T̂ cost | T̃ cost || T0=15: cost | T̂ cost | T̃ cost"
    )
    for hh, seed, r in e2_rows:
        a, b = r[2], r[15]
        print(
            f"   {hh:16} {seed:4} | {r['T*']:2} {r['cost*']:4.2f} | {a['corr/day']:6.1f} {a['cost_T0']:4.2f} | "
            f"{a['T']:2} {a['cost_hat']:4.2f} | {a['Tfit']:2} {a['cost_fit']:4.2f} || {b['cost_T0']:11.2f} | "
            f"{b['T']:2} {b['cost_hat']:4.2f} | {b['Tfit']:2} {b['cost_fit']:4.2f}"
        )


if __name__ == "__main__":
    main()
