"""Deterministic history analysis (M5): habits, undone automations, automations that fight.
No model is involved here; what it finds becomes a *finding*, and only findings with enough
repeats are handed to the orchestrator (core/review.py), which drafts a proposal from them.

Source: Home Assistant's logbook (`GET /api/logbook/<start>`), because unlike the history it says
what caused a change (field names [UNVERIFIED-ON-HW], docs/OWNER_CHECKLIST.md M5):
- `context_event_type: automation_triggered` / `context_domain: automation` → an automation
  (`context_entity_id` names it); `script` likewise;
- `context_user_id` of our own token (learned from a call_service answer) or a change that matches
  one of our executed actions → one of our actors (the house manager, the learner);
- a `context_user_id` of a program (Node-RED, AppDaemon, pyscript by name, or listed in the
  `automation_users` setting) → an automation (`user:<name>`), not the owner's hand;
- another `context_user_id` → the owner in the UI or an app;
- no context at all → a physical switch (or the device itself; HA cannot tell these apart).
"Manual" = UI/app or physical.

1. Habits: `core/habits.py` picks the devices worth a look; `core/predict.py` (D35) predicts, per
   device and state, whether the owner would set it now, checks each rule out of sample and decides
   act / ask / wait; the rules worth it become findings with a ready automation.
2. Feedback: a change made by one of our `agent_house_` automations that the owner reverts by hand
   within `UNDO_S`. `UNDO_MIN` undos and a rate of `UNDO_RATE` or more → a fix is drafted; a rate of
   `ROLLBACK_RATE` or more suggests a rollback (the owner decides, nothing is rolled back here).
3. Conflicts: two different automated causes (at least one of them ours) setting one entity to
   different states within `FIGHT_S`, at least `FIGHT_MIN` times → one finding per entity and pair.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..ha.client import AUTOMATION_PREFIX
from . import clock, predict
from .habits import ACT_DOMAINS
from .lifecycle import to_yaml

log = logging.getLogger(__name__)

SKIP_STATES = frozenset({"unavailable", "unknown"})
AGENT_MATCH_S = 10.0
UNDO_S = 600.0
UNDO_MIN = 3
UNDO_RATE = 0.3
ROLLBACK_RATE = 0.6
FIGHT_S = 120.0
FIGHT_MIN = 2
FEEDBACK_DAYS = 7
# HA users that are programs. Not voice assistants or HomeKit: there a person speaks or taps.
PROGRAM_USERS = re.compile(r"node[\s_-]?red|appdaemon|pyscript", re.I)


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


def classify(
    entry: dict, own_user: str | None = None, programs: dict | None = None
) -> tuple[str, str | None]:
    """(cause, ref). `programs`: HA user id → name of users that are programs (Node-RED …)."""
    event, domain = entry.get("context_event_type"), entry.get("context_domain")
    ref, user = entry.get("context_entity_id"), entry.get("context_user_id")
    if event == "automation_triggered" or domain == "automation":
        return "automation", ref
    if event == "script_started" or domain == "script":
        return "script", ref
    if user and own_user and user == own_user:
        return "agent", None
    if user and programs and user in programs:
        return "automation", f"user:{programs[user]}"
    if user:
        return "user", None
    if event or domain or ref:
        return "other", ref
    return "physical", None


def fires_from_logbook(entries: list[dict]) -> dict[str, list[float]]:
    """automation entity → when it was triggered: logbook entries of an automation carry no state,
    only "triggered by …" [UNVERIFIED-ON-HW]."""
    out: dict[str, list[float]] = defaultdict(list)
    for entry in entries:
        entity_id = entry.get("entity_id")
        if isinstance(entity_id, str) and entity_id.startswith("automation.") and entry.get("state") is None:
            ts = _ts(entry.get("when"))
            if ts is not None:
                out[entity_id].append(ts)
    return dict(out)


def changes_from_logbook(
    entries: list[dict], own_user: str | None, actions: list[dict], programs: dict | None = None
) -> list[Change]:
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
        cause, ref = classify(entry, own_user, programs)
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


# --- 2. feedback after deploy ----------------------------------------------------------------
def find_undos(changes: list[Change], ours: dict[str, dict]) -> list[dict]:
    """`ours`: automation entity → {ha_id, version, since}. One result per automation that acted."""
    by_entity: dict[str, list[Change]] = defaultdict(list)
    for change in changes:
        by_entity[change.entity_id].append(change)
    stats: dict[str, dict] = {}
    for entity_id, rows in by_entity.items():
        limits = predict.undo_limits(rows)  # switched off after its usual two minutes is use, not an undo
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
                and following.ts - change.ts <= limits.get(change.state, UNDO_S)
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


def conflict_request(item: dict, ours: dict[str, dict]) -> tuple[str, str, str | None, list[str]]:
    """(title, request, ha_id of ours to change, other automations of ours it supersedes).

    Two of ours fight: the draft replaces the first and supersedes the other (disabled on
    approval). The other side is the owner's automation, a script or an actor of ours: nothing of theirs
    is touched; the request says so and the owner can switch theirs off from Migration."""
    refs = [side.split(":", 1)[1] for side in item["sides"]]
    mine = next((ours[r]["ha_id"] for r in refs if r in ours), None)
    supersedes = sorted({ours[r]["ha_id"] for r in refs if r in ours} - {mine})
    others = [r for r in refs if r not in ours and r.split(".", 1)[0] in ("automation", "script")]
    agents = [r for r in refs if r not in ours and r not in others]
    request = (
        f"{item['entity_id']} is switched back and forth by {' and '.join(refs)} within "
        f"{int(FIGHT_S)} s ({item['count']} times: " + "; ".join(item["examples"]) + "). "
        f"Change our automation {mine} so that it alone decides this entity."
    )
    if supersedes:
        request += (
            f" Merge what {', '.join(supersedes)} does for {item['entity_id']} into it: "
            f"{', '.join(supersedes)} will be switched off when the owner approves."
        )
    if others:
        request += (
            f" {', '.join(others)} is not ours and stays as it is: avoid fighting it (a condition, a "
            "time window or the house mode), and say in the rationale that the owner can switch "
            f"{', '.join(others)} off from the Migration page if ours should decide."
        )
    if agents:
        request += f" {', '.join(agents)} (ours, acting live) also acts on it: do not fight its choices."
    return f"Conflict on {item['entity_id']}: {' vs '.join(refs)}", request, mine, supersedes


# --- the scan --------------------------------------------------------------------------------
class Insights:
    """Reads the logbook, runs the three analyses and keeps the results in `findings`."""

    def __init__(self, store, feed, ha, lifecycle, settings) -> None:
        self.store, self.feed, self.ha, self.lifecycle, self.settings = store, feed, ha, lifecycle, settings
        # D38: () → (predictions, funnel) of the learner's longer history while fresh, else None
        self.learnt = lambda: None

    async def changes(self, days: int, now: datetime | None = None) -> list[Change]:
        end = now or clock.now(UTC)
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
        own = getattr(self.ha, "own_user_id", None)
        self.fires = fires_from_logbook(entries)  # when our asks asked (D35)
        return changes_from_logbook(entries, own, actions, await self.programs(own))

    async def programs(self, own: str | None = None) -> dict[str, str]:
        """HA user id → name, for users that are programs: named like one (Node-RED …) in HA's user
        list, or listed in `automation_users` by id or name. The list needs an admin token; without
        it only ids from the setting count [UNVERIFIED-ON-HW]."""
        listed = [x.strip() for x in (getattr(self.settings, "automation_users", None) or "").split(",")]
        listed = [x for x in listed if x]
        names = {x.casefold() for x in listed}
        out = {x: x for x in listed if re.fullmatch(r"[0-9a-f]{32}", x)}
        try:
            users = await self.ha.command("config/auth/list")
        except Exception as err:  # noqa: BLE001 - not an admin token, no WS, a stub: the setting alone
            log.debug("HA user list unavailable: %s", err)
            users = []
        for user in users if isinstance(users, list) else []:
            uid, name = user.get("id"), str(user.get("name") or "")
            if uid and uid != own and (PROGRAM_USERS.search(name) or name.casefold() in names or uid in out):
                out[uid] = name or uid
        return out

    def ours(self) -> dict[str, dict]:
        """automation entity → {ha_id, version, since, title, yaml} for our deployed automations."""
        out = {}
        for row in self.store.query(
            "SELECT d.ha_id, d.title, d.current_version, v.created, v.yaml FROM deployed d "
            "JOIN automation_versions v ON v.ha_id=d.ha_id AND v.version=d.current_version WHERE d.removed=0"
        ):
            entity = self.ha.automation_entity(row["ha_id"])
            if entity and row["ha_id"].startswith(AUTOMATION_PREFIX):
                out[entity] = {"ha_id": row["ha_id"], "version": row["current_version"],
                               "since": row["created"], "title": row["title"],
                               "yaml": row["yaml"]}  # fmt: skip
        return out

    async def scan(self, now: datetime | None = None) -> dict:
        days = int(getattr(self.settings, "habit_days", 14))
        min_days = int(getattr(self.settings, "habit_min_repeats", 3))
        changes = await self.changes(days, now)
        area_of = self.ha.registry.area_of
        with_yaml = self.ours()
        ours = {e: {k: v for k, v in i.items() if k != "yaml"} for e, i in with_yaml.items()}
        recent = (now or clock.now(UTC)).timestamp() - FEEDBACK_DAYS * 86400
        latest = [c for c in changes if c.ts >= recent]
        policy = getattr(getattr(self.lifecycle, "safety", None), "policy", None)
        mine = {
            entity: {"ha_id": info["ha_id"], "targets": predict.acted_states(info.get("yaml") or ""),
                     "ask": predict.ASK_EVENT in (info.get("yaml") or "")}
            for entity, info in with_yaml.items()
        }  # fmt: skip
        end = (now or clock.now(UTC)).timestamp()
        learnt = self.learnt()
        if learnt is not None:  # the same D35 code on the Jetson, over months instead of days
            preds, funnel = learnt
            funnel = {**funnel, "source": "learner"}
        else:
            preds, funnel = predict.scan(changes, self.ha, days=days, min_days=min_days, policy=policy,
                                         ours=mine, end=end, fires=getattr(self, "fires", {}))  # fmt: skip
        self.store.set("usual_states", predict.usual_states(changes, self.ha, days=days, min_days=min_days,
                                                            policy=policy, end=end))  # fmt: skip
        found = []
        for p in preds:
            title = f"{p['name'] or p['entity_id']} {predict.summary(p)}"[:200]
            config = p.pop("yaml_config")
            data = {**p, "yaml": to_yaml(config)}
            key = f"habit:{p['entity_id']}:{p['state']}:{p['mode']}"
            found.append(self._save(key, "habit", title, predict.request(p), data, p["room"]))
        undos = find_undos(latest, ours)
        for item in undos:
            if item["flagged"]:
                title, request = undo_request(item, item["title"])
                key = f"undo:{item['ha_id']}:v{item['version']}"
                found.append(self._save(key, "undo", title, request, item, None, item["ha_id"]))
        for item in find_conflicts(latest, ours):
            title, request, ha_id, supersedes = conflict_request(item, ours)
            theirs = [
                ref for ref in (side.split(":", 1)[1] for side in item["sides"])
                if ref not in ours and ref.split(".", 1)[0] in ("automation", "script")
            ]  # fmt: skip
            item = {**item, "supersedes": supersedes, "theirs": theirs}
            key = f"conflict:{item['entity_id']}:{'|'.join(item['sides'])}"
            found.append(self._save(key, "conflict", title, request, item, area_of(item["entity_id"]), ha_id))
        summary = {
            "changes": len(changes),
            "manual": sum(c.manual for c in changes),
            "findings": [f for f in found if f],
            "feedback": [{k: i[k] for k in ("ha_id", "acted", "undone", "rate", "flagged")} for i in undos],
            "habits": {**funnel, "days": days, "min_days": min_days},  # D35 predictions
            "scanned": clock.time(),
        }
        self.store.set("insights_last_scan", {k: v for k, v in summary.items() if k != "findings"})
        self.feed.add(
            "council",
            f"History scan: {summary['changes']} changes ({summary['manual']} by hand); habits: "
            f"{funnel['relevant']} of {funnel['entities']} entities relevant, {funnel['predictions']} worth "
            "acting on or asking; "
            f"{len(summary['findings'])} findings",
        )
        return summary

    def _save(self, key, kind, title, request, data, room, ha_id=None) -> dict:
        now = clock.time()
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
            (status, *extra.values(), clock.time(), finding_id),
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
