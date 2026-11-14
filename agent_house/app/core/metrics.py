"""Accuracy of every automation of ours and of every room agent, and one score for the whole house.

Deterministic, no model. Source: the same logbook changes as the history scan (`insights.py`),
so a cause is what HA says it was. Each automated change is an outcome with implicit feedback:

- kept      (true positive)   the owner did not revert it by hand within `UNDO_S`;
- undone    (false positive)  the owner set the entity back by hand within `UNDO_S`;
- unobserved                  nobody was home when it acted (all `person.*` away): the owner could
                              not have reverted it, so it counts neither way (no feedback is not
                              approval, and counting it as kept would inflate precision);
- missed    (false negative)  the owner set one of the automation's targets *by hand* to a state
                              the automation sets (e.g. light on) and the automation had not done it
                              within `UNDO_S` before: work the automation could do and did not.

Per automation:  precision = kept / (kept + undone),  coverage = kept / (kept + missed),
                 f1 = harmonic mean of the two.
Room agents get precision only (they act on events; there is no fixed intent to miss).

The house (program) score pools the counts (micro-average, so a busy automation weighs more than
one that fired twice): precision over automations and agents, coverage over automations, and
`score` = F1 of the two. Every rate comes with a 95 % Wilson interval; `score_low` is the F1 of the
two lower bounds, the conservative number to watch. Below `MIN_N` outcomes a rate is `None`
(insufficient data), never 0 or 1. `house_share` = automated kept changes / (those + every manual
change of a switchable device): how much of the house's switching no longer needs the owner, the
long-run goal; it cannot reach 1 (not everything the owner does should be automated), so watch its
trend, not its level. Limits (docs/POLYGON.md "Metrics"): an undo after `UNDO_S` is not seen; a
missed change may have been a one-off the owner would not want automated; presence comes from
`person.*` only.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable

import yaml

from .insights import ACT_DOMAINS, UNDO_S, Change
from .lifecycle import _action_key, _as_list, normalize

MIN_N = 5  # outcomes below which a rate is "insufficient data"
WEAK = 0.6  # precision or coverage under which an automation is worth a look
Z = 1.96  # 95 %
STATE_OF = {  # service → the state it leaves the entity in (toggle and friends have none)
    "turn_on": "on", "turn_off": "off", "open_cover": "open", "close_cover": "closed",
    "lock": "locked", "unlock": "unlocked", "media_play": "playing", "media_pause": "paused",
    "media_stop": "idle",
}  # fmt: skip


def wilson(successes: int, n: int, z: float = Z) -> tuple[float, float] | None:
    """95 % Wilson score interval of a proportion; None without data. Correct coverage at small n,
    unlike the normal approximation (Agresti & Coull 1998)."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def rate(successes: int, n: int) -> dict:
    interval = wilson(successes, n)
    enough = n >= MIN_N
    return {
        "value": round(successes / n, 3) if enough else None,
        "low": round(interval[0], 3) if enough and interval else None,
        "high": round(interval[1], 3) if enough and interval else None,
        "n": n,
    }


def is_weak(item: dict) -> bool:
    """Point estimate (with at least MIN_N outcomes) under WEAK. Not the Wilson lower bound: with
    5 of 5 kept it is 0.57, so every young automation would look weak."""
    return any((item[k]["value"] is not None and item[k]["value"] < WEAK) for k in ("precision", "coverage"))


def f1(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return round(2 * a * b / (a + b), 3) if a + b > 0 else 0.0


def intended_states(text: str) -> dict[str, set[str]]:
    """entity → the states the automation's actions set it to (only services with a known result)."""
    try:
        config = normalize(yaml.safe_load(text) or {})
    except (yaml.YAMLError, AttributeError, TypeError):
        return {}
    out: dict[str, set[str]] = defaultdict(set)

    def walk(steps) -> None:
        for step in steps if isinstance(steps, list) else [steps]:
            if not isinstance(step, dict):
                continue
            step = _action_key(step)
            action = step.get("action")
            if isinstance(action, str) and "." in action:
                domain, service = action.split(".", 1)
                data = step.get("data") or {}
                state = STATE_OF.get(service)
                if service == "set_hvac_mode" and isinstance(data, dict):
                    state = data.get("hvac_mode")
                if service == "select_option" and isinstance(data, dict):
                    state = data.get("option")
                if isinstance(state, str):
                    for source in ((step.get("target") or {}), data, step):
                        if isinstance(source, dict):
                            for e in _as_list(source.get("entity_id")):
                                if isinstance(e, str) and e.startswith(f"{domain}."):
                                    out[e].add(state)
            for key in ("then", "else", "sequence", "default", "parallel"):
                if key in step:
                    walk(step[key])
            for option in _as_list(step.get("choose")):
                if isinstance(option, dict):
                    walk(option.get("sequence", []))
            if isinstance(step.get("repeat"), dict):
                walk(step["repeat"].get("sequence", []))

    walk(config.get("actions") or [])
    return dict(out)


class _Presence:
    """Was anybody home at time t? From person.* changes; unknown counts as home (observed)."""

    def __init__(self, changes: Iterable[Change]) -> None:
        self.events = sorted(
            (c.ts, c.entity_id, c.state) for c in changes if c.entity_id.startswith("person.")
        )

    def anybody_home(self, ts: float) -> bool:
        state: dict[str, str] = {}
        for t, entity, value in self.events:
            if t > ts:
                break
            state[entity] = value
        return not state or any(v == "home" for v in state.values())


def evaluate(changes: list[Change], ours: dict[str, dict], intents: dict[str, dict[str, set[str]]]) -> dict:
    """`ours`: automation entity → {ha_id, since, title, version}; `intents`: ha_id → intended_states.
    Pure: everything the API and the review show comes from here."""
    by_entity: dict[str, list[Change]] = defaultdict(list)
    for change in changes:
        by_entity[change.entity_id].append(change)
    presence = _Presence(changes)
    autos: dict[str, dict] = {
        info["ha_id"]: {"ha_id": info["ha_id"], "title": info.get("title"), "version": info.get("version"),
                        "kept": 0, "undone": 0, "unobserved": 0, "missed": 0, "examples": []}
        for info in ours.values()
    }  # fmt: skip
    agents: dict[str, dict] = {}
    undo_of: set[int] = set()  # id() of manual changes that reverted something of ours
    acted_at: dict[tuple[str, str], list[float]] = defaultdict(list)  # (ha_id, entity) → times

    for entity_id, rows in by_entity.items():
        for i, change in enumerate(rows):
            if change.cause == "automation" and change.ref in ours:
                info = ours[change.ref]
                if change.ts < info["since"]:
                    continue
                item = autos[info["ha_id"]]
                acted_at[(info["ha_id"], entity_id)].append(change.ts)
            elif change.cause == "agent" and change.ref:
                item = agents.setdefault(change.ref, {"agent_id": change.ref, "kept": 0, "undone": 0,
                                                      "unobserved": 0, "examples": []})  # fmt: skip
            else:
                continue
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            if nxt is not None and nxt.manual and nxt.ts - change.ts <= UNDO_S and nxt.state != change.state:
                item["undone"] += 1
                undo_of.add(id(nxt))
                if len(item["examples"]) < 3:
                    item["examples"].append(
                        f"{entity_id} {change.state} undone by hand after {int(nxt.ts - change.ts)} s"
                    )
            elif not presence.anybody_home(change.ts):
                item["unobserved"] += 1
            else:
                item["kept"] += 1

    for info in ours.values():
        ha_id = info["ha_id"]
        for entity_id, states in (intents.get(ha_id) or {}).items():
            for change in by_entity.get(entity_id, []):
                if (
                    not change.manual or change.ts < info["since"] or id(change) in undo_of
                    or change.state not in states
                ):  # fmt: skip
                    continue
                if any(0 <= change.ts - t <= UNDO_S for t in acted_at.get((ha_id, entity_id), [])):
                    continue  # it acted shortly before: the owner repeated it, not a miss
                autos[ha_id]["missed"] += 1
                if len(autos[ha_id]["examples"]) < 3:
                    autos[ha_id]["examples"].append(f"{entity_id} → {change.state} by hand (missed)")

    for item in autos.values():
        item["precision"] = rate(item["kept"], item["kept"] + item["undone"])
        item["coverage"] = rate(item["kept"], item["kept"] + item["missed"])
        item["f1"] = f1(item["precision"]["value"], item["coverage"]["value"])
    for item in agents.values():
        item["precision"] = rate(item["kept"], item["kept"] + item["undone"])

    kept_a = sum(i["kept"] for i in autos.values())
    kept_g = sum(i["kept"] for i in agents.values())
    undone = sum(i["undone"] for i in autos.values()) + sum(i["undone"] for i in agents.values())
    missed = sum(i["missed"] for i in autos.values())
    precision = rate(kept_a + kept_g, kept_a + kept_g + undone)
    coverage = rate(kept_a, kept_a + missed)
    manual = sum(1 for c in changes if c.manual and c.entity_id.split(".", 1)[0] in ACT_DOMAINS)
    program = {
        "precision": precision,
        "coverage": coverage,
        "score": f1(precision["value"], coverage["value"]),
        "score_low": f1(precision["low"], coverage["low"]),
        "house_share": rate(kept_a + kept_g, kept_a + kept_g + manual),
        "counts": {"kept": kept_a + kept_g, "undone": undone, "missed": missed, "manual": manual,
                   "unobserved": sum(i["unobserved"] for i in [*autos.values(), *agents.values()])},
    }  # fmt: skip
    return {
        "automations": sorted(autos.values(), key=lambda i: (i["f1"] is None, i["f1"] or 0)),
        "agents": sorted(agents.values(), key=lambda i: i["agent_id"]),
        "program": program,
    }


class Metrics:
    """Reads the logbook through `insights` and evaluates; keeps a daily history for the trend."""

    HISTORY_KEY = "metrics_history"
    HISTORY_DAYS = 90

    def __init__(self, store, insights, lifecycle) -> None:
        self.store, self.insights, self.lifecycle = store, insights, lifecycle

    def intents(self) -> dict[str, dict[str, set[str]]]:
        rows = self.store.query(
            "SELECT d.ha_id, v.yaml FROM deployed d JOIN automation_versions v ON v.ha_id=d.ha_id "
            "AND v.version=d.current_version WHERE d.removed=0"
        )
        return {r["ha_id"]: intended_states(r["yaml"]) for r in rows}

    async def compute(self, days: int = 7) -> dict:
        changes = await self.insights.changes(days)
        result = evaluate(changes, self.insights.ours(), self.intents())
        return {"days": days, "min_n": MIN_N, "undo_window_s": UNDO_S, **result,
                "history": self.history()}  # fmt: skip

    def history(self) -> list[dict]:
        return list(self.store.get(self.HISTORY_KEY) or [])

    def snapshot(self, day: str, result: dict) -> None:
        p = result["program"]
        row = {"day": day, "score": p["score"], "score_low": p["score_low"],
               "precision": p["precision"]["value"], "coverage": p["coverage"]["value"],
               "house_share": p["house_share"]["value"], **p["counts"]}  # fmt: skip
        rows = [r for r in self.history() if r.get("day") != day] + [row]
        self.store.set(self.HISTORY_KEY, rows[-self.HISTORY_DAYS :])
