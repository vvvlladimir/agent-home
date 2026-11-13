"""Deterministic history analysis (M5): habits, undone automations, automations that fight.
No model is involved here; what it finds becomes a *finding*, and only findings with enough
repeats are handed to the orchestrator (core/review.py), which drafts a proposal from them.

Source: Home Assistant's logbook (`GET /api/logbook/<start>`), because unlike the history it says
what caused a change (field names [UNVERIFIED-ON-HW], docs/OWNER_CHECKLIST.md M5):
- `context_event_type: automation_triggered` / `context_domain: automation` → an automation
  (`context_entity_id` names it); `script` likewise;
- `context_user_id` of our own token (learned from a call_service answer) or a change that matches
  one of our executed actions → a room agent;
- another `context_user_id` → the owner in the UI or an app;
- no context at all → a physical switch (or the device itself; HA cannot tell these apart).
"Manual" = UI/app or physical.

1. Habit finder: a manual change of a device (entity → state) that
   a) follows a sensor change (binary_sensor, person, sun, house mode) within `FOLLOW_S`, on at
      least `min_days` different days, where the sensor change was followed by it at least
      `MIN_PRECISION` of the time and it followed the sensor at least `MIN_RECALL` of the time; or
   b) happens at about the same time of day (a `TIME_WINDOW_MIN` window) on at least `min_days`
      days and on at least half of the days scanned.
2. Feedback: a change made by one of our `agent_house_` automations that the owner reverts by hand
   within `UNDO_S`. `UNDO_MIN` undos and a rate of `UNDO_RATE` or more → a fix is drafted; a rate of
   `ROLLBACK_RATE` or more suggests a rollback (the owner decides, nothing is rolled back here).
3. Conflicts: two different automated causes (at least one of them ours) setting one entity to
   different states within `FIGHT_S`, at least `FIGHT_MIN` times → one finding per entity and pair.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..ha.client import AUTOMATION_PREFIX

log = logging.getLogger(__name__)

ACT_DOMAINS = frozenset(
    {"light", "switch", "fan", "cover", "media_player", "climate", "humidifier", "input_boolean"}
)
TRIGGER_DOMAINS = frozenset({"binary_sensor", "person", "sun", "input_select"})
HOUSE_WIDE = frozenset({"person", "sun", "input_select"})  # triggers for every room
SKIP_STATES = frozenset({"unavailable", "unknown"})
FOLLOW_S = 180.0
MIN_PRECISION = 0.3
MIN_RECALL = 0.5
TIME_WINDOW_MIN = 40
MIN_DAY_SHARE = 0.5
AGENT_MATCH_S = 10.0
UNDO_S = 600.0
UNDO_MIN = 3
UNDO_RATE = 0.3
ROLLBACK_RATE = 0.6
FIGHT_S = 120.0
FIGHT_MIN = 2
FEEDBACK_DAYS = 7


@dataclass(frozen=True)
class Change:
    ts: float
    entity_id: str
    state: str
    cause: str  # user | physical | agent | automation | script | other
    ref: str | None = None  # automation.x / script.x / agent id

    @property
    def manual(self) -> bool:
        return self.cause in ("user", "physical")


def _ts(value) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).timestamp()


def classify(entry: dict, own_user: str | None = None) -> tuple[str, str | None]:
    event, domain = entry.get("context_event_type"), entry.get("context_domain")
    ref, user = entry.get("context_entity_id"), entry.get("context_user_id")
    if event == "automation_triggered" or domain == "automation":
        return "automation", ref
    if event == "script_started" or domain == "script":
        return "script", ref
    if user and own_user and user == own_user:
        return "agent", None
    if user:
        return "user", None
    if event or domain or ref:
        return "other", ref
    return "physical", None


def changes_from_logbook(entries: list[dict], own_user: str | None, actions: list[dict]) -> list[Change]:
    """Logbook entries → state changes with their cause, oldest first. `actions`: our executed
    actions ({ts, agent_id, entity_id}); a change of that entity within seconds is the agent's."""
    ours = defaultdict(list)
    for action in actions:
        ours[action["entity_id"]].append((action["ts"], action["agent_id"]))
    out = []
    for entry in entries:
        entity_id, state = entry.get("entity_id"), entry.get("state")
        ts = _ts(entry.get("when"))
        if not isinstance(entity_id, str) or state is None or ts is None or str(state) in SKIP_STATES:
            continue
        cause, ref = classify(entry, own_user)
        if cause in ("user", "physical"):
            match = next((a for t, a in ours.get(entity_id, []) if abs(t - ts) <= AGENT_MATCH_S), None)
            if match:
                cause, ref = "agent", match
        out.append(Change(ts, entity_id, str(state), cause, ref))
    out.sort(key=lambda c: c.ts)
    return out


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().date().isoformat()


def _minute(ts: float) -> int:
    moment = datetime.fromtimestamp(ts).astimezone()
    return moment.hour * 60 + moment.minute


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


# --- 1. habits --------------------------------------------------------------------------------
def find_habits(changes: list[Change], area_of, days: int, min_days: int) -> list[dict]:
    """Habit candidates, strongest first. `area_of(entity_id) -> area | None`."""
    manual: dict[tuple[str, str], list[Change]] = defaultdict(list)
    triggers: dict[tuple[str, str], list[float]] = defaultdict(list)
    previous: dict[str, str] = {}
    for change in changes:
        domain = _domain(change.entity_id)
        if domain in ACT_DOMAINS and change.manual:
            manual[(change.entity_id, change.state)].append(change)
        if domain in TRIGGER_DOMAINS and previous.get(change.entity_id) != change.state:
            triggers[(change.entity_id, change.state)].append(change.ts)
        previous[change.entity_id] = change.state
    found = []
    for (entity_id, state), events in manual.items():
        if len({_day(e.ts) for e in events}) < min_days:
            continue
        habit = _follow_habit(entity_id, state, events, triggers, area_of, min_days)
        habit = habit or _time_habit(entity_id, state, events, days, min_days)
        if habit:
            habit["room"] = area_of(entity_id)
            found.append(habit)
    found.sort(key=lambda h: (-h["days"], -h["count"]))
    return found


def _follow_habit(entity_id, state, events, triggers, area_of, min_days) -> dict | None:
    area = area_of(entity_id)
    best = None
    for (trigger, trigger_state), times in triggers.items():
        if trigger == entity_id:
            continue
        if _domain(trigger) not in HOUSE_WIDE and (area is None or area_of(trigger) != area):
            continue
        followed, delays = [], []
        for event in events:
            before = [t for t in times if 0 <= event.ts - t <= FOLLOW_S]
            if before:
                followed.append(event)
                delays.append(event.ts - max(before))
        days = len({_day(e.ts) for e in followed})
        if days < min_days:
            continue
        hits = sum(1 for t in times if any(0 <= e.ts - t <= FOLLOW_S for e in events))
        precision, recall = hits / len(times), len(followed) / len(events)
        if precision < MIN_PRECISION or recall < MIN_RECALL:
            continue
        score = precision * recall
        if best is None or score > best["score"]:
            best = {
                "kind": "follow", "entity_id": entity_id, "state": state, "trigger": trigger,
                "trigger_state": trigger_state, "count": len(followed), "days": days,
                "trigger_count": len(times), "precision": round(precision, 2), "recall": round(recall, 2),
                "median_delay_s": round(statistics.median(delays)), "score": score,
            }  # fmt: skip
    return best


def _time_habit(entity_id, state, events, days, min_days) -> dict | None:
    minutes = sorted(((_minute(e.ts), e) for e in events), key=lambda pair: (pair[0], pair[1].ts))
    best: list = []
    for start, _ in minutes:
        window = [(m, e) for m, e in minutes if (m - start) % 1440 < TIME_WINDOW_MIN]
        if len({_day(e.ts) for _, e in window}) > len({_day(e.ts) for _, e in best}):
            best = window
    on_days = sorted({_day(e.ts) for _, e in best})
    if len(on_days) < min_days or len(on_days) < MIN_DAY_SHARE * days:
        return None
    center = int(statistics.median((m - best[0][0]) % 1440 for m, _ in best) + best[0][0]) % 1440
    weekdays = sorted({datetime.fromtimestamp(e.ts).astimezone().weekday() for _, e in best})
    return {
        "kind": "time", "entity_id": entity_id, "state": state, "at": f"{center // 60:02d}:{center % 60:02d}",
        "count": len(best), "days": len(on_days), "days_scanned": days, "weekdays": weekdays,
        "score": len(on_days) / days,
    }  # fmt: skip


def habit_request(habit: dict) -> tuple[str, str]:
    """(title, request for the orchestrator) of one habit."""
    entity, state = habit["entity_id"], habit["state"]
    if habit["kind"] == "follow":
        title = f"{entity} → {state} after {habit['trigger']} → {habit['trigger_state']}"
        facts = (
            f"{entity} was set to {state} by hand within {int(FOLLOW_S // 60)} minutes after "
            f"{habit['trigger']} changed to {habit['trigger_state']}: {habit['count']} times on "
            f"{habit['days']} days; the sensor changed to {habit['trigger_state']} {habit['trigger_count']} "
            f"times and was followed by it {int(habit['precision'] * 100)}% of the time; median delay "
            f"{habit['median_delay_s']} s."
        )
    else:
        title = f"{entity} → {state} at about {habit['at']}"
        days = (
            "every day"
            if len(habit["weekdays"]) == 7
            else "on weekdays "
            + ",".join("Mon Tue Wed Thu Fri Sat Sun".split()[d] for d in habit["weekdays"])
        )
        facts = (
            f"{entity} was set to {state} by hand at about {habit['at']} ({days}) on {habit['days']} of "
            f"the last {habit['days_scanned']} days."
        )
    request = (
        f"A habit found in Home Assistant's history (counted, no model involved): {facts} "
        "Draft one automation that does this for the owner. Use the trigger named here; add a "
        "condition only if it is clearly needed (for example the house mode, input_select."
        "agent_house_mode, or that it is dark)."
    )
    return title, request


# --- 2. feedback after deploy ----------------------------------------------------------------
def find_undos(changes: list[Change], ours: dict[str, dict]) -> list[dict]:
    """`ours`: automation entity → {ha_id, version, since}. One result per automation that acted."""
    by_entity: dict[str, list[Change]] = defaultdict(list)
    for change in changes:
        by_entity[change.entity_id].append(change)
    stats: dict[str, dict] = {}
    for entity_id, rows in by_entity.items():
        for index, change in enumerate(rows):
            info = ours.get(change.ref or "") if change.cause == "automation" else None
            if info is None or change.ts < info["since"]:
                continue
            item = stats.setdefault(change.ref, {**info, "automation": change.ref, "acted": 0, "undone": 0,
                                                 "examples": []})  # fmt: skip
            item["acted"] += 1
            following = rows[index + 1] if index + 1 < len(rows) else None
            if (
                following is not None
                and following.manual
                and following.ts - change.ts <= UNDO_S
                and following.state != change.state
            ):
                item["undone"] += 1
                if len(item["examples"]) < 5:
                    delay = int(following.ts - change.ts)
                    item["examples"].append(
                        f"{datetime.fromtimestamp(change.ts).astimezone():%a %H:%M} {entity_id} "
                        f"{change.state} → {following.state} by hand after {delay // 60} min {delay % 60} s"
                    )
    out = []
    for item in stats.values():
        item["rate"] = round(item["undone"] / item["acted"], 2) if item["acted"] else 0.0
        item["flagged"] = item["undone"] >= UNDO_MIN and item["rate"] >= UNDO_RATE
        item["suggest_rollback"] = item["flagged"] and item["rate"] >= ROLLBACK_RATE and item["version"] > 1
        out.append(item)
    return sorted(out, key=lambda i: -i["rate"])


def undo_request(item: dict, title: str) -> tuple[str, str]:
    request = (
        f"The deployed automation {item['ha_id']} ({title}) acted {item['acted']} times in the last "
        f"{FEEDBACK_DAYS} days and the owner undid it by hand {item['undone']} times within "
        f"{int(UNDO_S // 60)} minutes ({int(item['rate'] * 100)}%). Examples: "
        + "; ".join(item["examples"])
        + ". Change this automation so the owner no longer has to undo it (a condition such as the "
        "house mode, a time window, a delay, or a different trigger). Keep what it is for."
    )
    return f"Undone {item['undone']}× of {item['acted']}: {title}", request


# --- 3. conflicts ----------------------------------------------------------------------------
def find_conflicts(changes: list[Change], ours: dict[str, dict]) -> list[dict]:
    automated = ("automation", "agent", "script")
    pairs: dict[tuple[str, str, str], dict] = {}
    last: dict[str, Change] = {}
    for change in changes:
        if _domain(change.entity_id) not in ACT_DOMAINS:
            continue
        before = last.get(change.entity_id)
        last[change.entity_id] = change
        if before is None or change.cause not in automated or before.cause not in automated:
            continue
        a, b = f"{before.cause}:{before.ref}", f"{change.cause}:{change.ref}"
        if a == b or change.state == before.state or change.ts - before.ts > FIGHT_S:
            continue
        if not any((r.split(":", 1)[1] in ours) for r in (a, b)):
            continue  # nothing of ours involved: the owner's own business
        first, second = sorted((a, b))
        item = pairs.setdefault((change.entity_id, first, second), {
            "entity_id": change.entity_id, "sides": [first, second], "count": 0, "examples": []})  # fmt: skip
        item["count"] += 1
        if len(item["examples"]) < 5:
            item["examples"].append(
                f"{datetime.fromtimestamp(change.ts).astimezone():%a %H:%M}: "
                f"{before.ref or before.cause} set {before.state}, {int(change.ts - before.ts)} s later "
                f"{change.ref or change.cause} set {change.state}"
            )
    return [p for p in pairs.values() if p["count"] >= FIGHT_MIN]


def conflict_request(item: dict, ours: dict[str, dict]) -> tuple[str, str, str | None]:
    """(title, request, ha_id of ours to change)."""
    refs = [side.split(":", 1)[1] for side in item["sides"]]
    mine = next((ours[r]["ha_id"] for r in refs if r in ours), None)
    others = [r for r in refs if not (r in ours and ours[r]["ha_id"] == mine)]
    request = (
        f"{item['entity_id']} is switched back and forth by {' and '.join(refs)} within "
        f"{int(FIGHT_S)} s ({item['count']} times: " + "; ".join(item["examples"]) + "). "
        f"Change our automation {mine} so the two no longer fight: one automation must decide this "
        f"entity (merge the logic of {', '.join(others)} into it if needed, and say in the rationale "
        "which one the owner should switch off)."
    )
    return f"Conflict on {item['entity_id']}: {' vs '.join(refs)}", request, mine


# --- the scan --------------------------------------------------------------------------------
class Insights:
    """Reads the logbook, runs the three analyses and keeps the results in `findings`."""

    def __init__(self, store, feed, ha, lifecycle, settings) -> None:
        self.store, self.feed, self.ha, self.lifecycle, self.settings = store, feed, ha, lifecycle, settings

    async def changes(self, days: int, now: datetime | None = None) -> list[Change]:
        end = now or datetime.now(UTC)
        entries: list[dict] = []
        for back in range(days, 0, -1):  # one request per day keeps each answer small
            start = end - timedelta(days=back)
            stop = min(end, start + timedelta(days=1))
            entries += await self.ha.logbook(start.isoformat(), stop.isoformat())
        since = (end - timedelta(days=days)).timestamp()
        actions = [
            {"ts": row["ts"], "agent_id": row["agent_id"], "entity_id": json.loads(row["entity_ids"])[0]}
            for row in self.store.query(
                "SELECT ts, agent_id, entity_ids FROM actions WHERE ts>=? AND verdict IN ('executed', "
                "'approved')",
                (since,),
            )
        ]
        return changes_from_logbook(entries, getattr(self.ha, "own_user_id", None), actions)

    def ours(self) -> dict[str, dict]:
        """automation entity → {ha_id, version, since, title} for our deployed automations."""
        out = {}
        for row in self.store.query(
            "SELECT d.ha_id, d.title, d.current_version, v.created FROM deployed d "
            "JOIN automation_versions v ON v.ha_id=d.ha_id AND v.version=d.current_version WHERE d.removed=0"
        ):
            entity = self.ha.automation_entity(row["ha_id"])
            if entity and row["ha_id"].startswith(AUTOMATION_PREFIX):
                out[entity] = {"ha_id": row["ha_id"], "version": row["current_version"],
                               "since": row["created"], "title": row["title"]}  # fmt: skip
        return out

    async def scan(self, now: datetime | None = None) -> dict:
        days = int(getattr(self.settings, "habit_days", 14))
        min_days = int(getattr(self.settings, "habit_min_repeats", 5))
        changes = await self.changes(days, now)
        area_of = self.ha.registry.area_of
        ours = self.ours()
        recent = (now or datetime.now(UTC)).timestamp() - FEEDBACK_DAYS * 86400
        latest = [c for c in changes if c.ts >= recent]
        automated = {e for info in ours.values() for e in _targets(self.lifecycle, info["ha_id"])}
        found = []
        for habit in find_habits(changes, area_of, days, min_days):
            if habit["entity_id"] in automated:
                continue  # one of ours already does something with it: feedback covers that
            title, request = habit_request(habit)
            key = f"habit:{habit['entity_id']}:{habit['state']}:" + (
                f"{habit['trigger']}:{habit['trigger_state']}" if habit["kind"] == "follow" else "time"
            )
            found.append(self._save(key, "habit", title, request, habit, habit["room"]))
        undos = find_undos(latest, ours)
        for item in undos:
            if item["flagged"]:
                title, request = undo_request(item, item["title"])
                key = f"undo:{item['ha_id']}:v{item['version']}"
                found.append(self._save(key, "undo", title, request, item, None, item["ha_id"]))
        for item in find_conflicts(latest, ours):
            title, request, ha_id = conflict_request(item, ours)
            key = f"conflict:{item['entity_id']}:{'|'.join(item['sides'])}"
            found.append(self._save(key, "conflict", title, request, item, area_of(item["entity_id"]), ha_id))
        summary = {
            "changes": len(changes),
            "manual": sum(c.manual for c in changes),
            "findings": [f for f in found if f],
            "feedback": [{k: i[k] for k in ("ha_id", "acted", "undone", "rate", "flagged")} for i in undos],
            "scanned": time.time(),
        }
        self.store.set("insights_last_scan", {k: v for k, v in summary.items() if k != "findings"})
        self.feed.add(
            "council",
            f"History scan: {summary['changes']} changes ({summary['manual']} by hand), "
            f"{len(summary['findings'])} findings",
        )
        return summary

    def _save(self, key, kind, title, request, data, room, ha_id=None) -> dict:
        now = time.time()
        clean = {k: v for k, v in data.items() if k != "score"}
        rows = self.store.query("SELECT status FROM findings WHERE id=?", (key,))
        if rows:
            self.store.execute(
                "UPDATE findings SET title=?, request=?, data=?, updated=? WHERE id=?",
                (title, request, json.dumps(clean), now, key),
            )
        else:
            self.store.execute(
                "INSERT INTO findings (id, kind, status, title, request, data, room, ha_id, created, "
                "updated) VALUES (?, ?, 'new', ?, ?, ?, ?, ?, ?, ?)",
                (key, kind, title, request, json.dumps(clean), room, ha_id, now, now),
            )
        return self.get(key)

    def get(self, finding_id: str) -> dict | None:
        rows = self.store.query("SELECT * FROM findings WHERE id=?", (finding_id,))
        return {**rows[0], "data": json.loads(rows[0]["data"])} if rows else None

    def list(self, status: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM findings", ()
        if status:
            sql, params = sql + " WHERE status=?", (status,)
        rows = self.store.query(sql + " ORDER BY updated DESC LIMIT 200", params)
        return [{**r, "data": json.loads(r["data"])} for r in rows]

    def set_status(self, finding_id: str, status: str, **extra) -> dict | None:
        sets = ", ".join(f"{k}=?" for k in ("status", *extra))
        self.store.execute(
            f"UPDATE findings SET {sets}, updated=? WHERE id=?",
            (status, *extra.values(), time.time(), finding_id),
        )
        return self.get(finding_id)


def _targets(lifecycle, ha_id: str) -> set[str]:
    from .lifecycle import action_entities

    rows = lifecycle.store.query(
        "SELECT v.yaml FROM deployed d JOIN automation_versions v ON v.ha_id=d.ha_id "
        "AND v.version=d.current_version WHERE d.ha_id=?",
        (ha_id,),
    )
    return action_entities(rows[0]["yaml"]) if rows else set()
