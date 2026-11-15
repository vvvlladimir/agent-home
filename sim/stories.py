"""Stories: a lived household plus what we inject, and what must be true afterwards (step 3).

Each story in sim/stories.yaml is lived k times with different seeds on a fresh polygon
(sim/days.py). A simple owner decides the drafts the way a person would: approve a draft whose
actions target one of the story's habits and name only entities HA has, reject the rest with a
reason. The report gives, per story, pass@1 (share of runs that passed) and pass^k (all k passed;
tau-bench's reliability measure), the failed expectations with house times, and a coverage table:
which features of the add-on the runs reached and which none did.

    python -m sim.stories list
    python -m sim.stories run --story all --k 2                       # fakes: model stories are n/a
    python -m sim.stories run --story morning_counter_habit --k 3 --room-llm claude --orchestrator claude
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from sim import days as days_mod
from sim import household, invariants, polygon

HERE = Path(__file__).resolve().parent
STORIES = HERE / "stories.yaml"


def load() -> dict:
    return yaml.safe_load(STORIES.read_text())["stories"]


# --- given: the household plus injections -----------------------------------------------------------
def resolve(pattern: str, entities: list[str]) -> str | None:
    if "*" not in pattern:
        return pattern if pattern in entities else None
    return next((e for e in sorted(entities) if fnmatch.fnmatch(e, pattern)), None)


def chaos_for(story: dict) -> household.Chaos:
    spec = story.get("chaos", "lifelike")
    if spec == "calm":
        return household.Chaos.calm()
    return replace(household.Chaos(), **(spec if isinstance(spec, dict) else {}))


def given(story: dict, start: datetime, seed: int, entities: list[str]) -> tuple[list[household.Event], dict]:
    """The household plan with the story's habits and incidents; also the resolved entity names."""
    events = household.plan(story["days"], start, seed=seed, chaos=chaos_for(story))
    rng, names, extra = random.Random(seed * 7919), {}, []
    for h in story.get("habits") or []:
        eid = resolve(h["entity"], entities)
        names[h["entity"]] = eid
        if eid is None:
            continue
        for d in range(story["days"]):
            day = start + timedelta(days=d)
            if h.get("days", "all") == "workdays" and day.weekday() >= 5:
                continue
            on, off = (_clock_at(day, h[k], h.get("sd_min", 15), rng) for k in ("on", "off"))
            on_state, off_state = h.get("on_state", "on"), h.get("off_state", "off")  # cover: open/closed
            extra += [
                household.Event(on.timestamp(), eid, on_state, "user", "habit"),
                household.Event(
                    max(off, on + timedelta(minutes=5)).timestamp(), eid, off_state, "user", "habit"
                ),
            ]
    for auto in story.get("owner_automations") or []:  # the owner's automation does it, not the hand
        eid = resolve(auto["entity"], entities)
        names[auto["entity"]] = eid
        for i, e in enumerate(events):
            day = datetime.fromtimestamp(e.ts, start.tzinfo)
            if (
                e.entity_id == eid and e.state == auto["state"] and e.cause == "user"
                and (auto.get("days", "all") != "workdays" or day.weekday() < 5)
            ):  # fmt: skip
                events[i] = replace(e, cause=f"automation:{auto['ref']}", tag="owner_automation")
    for inc in story.get("incidents") or []:
        eid = resolve(inc["entity"], entities)
        names[inc["entity"]] = eid
        if eid is None:
            continue
        if "on_tag_note" in inc:  # relative to a household event, e.g. "trip starts"
            anchor = next((e for e in events if e.note == inc["on_tag_note"]), None)
            if anchor is None:
                continue
            t = datetime.fromtimestamp(anchor.ts + 60 * inc.get("after_min", 0), start.tzinfo)
        else:
            t = _clock_at(start + timedelta(days=inc["day"]), inc["at"], 0, rng)
        extra.append(household.Event(t.timestamp(), eid, inc["state"], "physical", inc["tag"]))
        if inc.get("back_after_min"):
            back = inc.get("back_state") or ("off" if inc["state"] == "on" else "on")
            end = t.timestamp() + 60 * inc["back_after_min"]
            # note "back": the end of the incident, never an anchor of report_about
            extra.append(household.Event(end, eid, back, "physical", inc["tag"], "back"))
    return sorted(events + extra), names


def owner_history(story: dict, ha, entities: list[str]) -> int:
    """The owner's automations existed before the story: the backfilled logbook shows them too
    (stories run 2026-11-14: the first look drafted the TV off from a history without them)."""
    n = 0
    for auto in story.get("owner_automations") or []:
        eid = resolve(auto["entity"], entities)
        for row in ha.logbook_rows:
            if (
                row.get("entity_id") != eid
                or row.get("state") != auto["state"]
                or not row.get("context_user_id")
            ):
                continue
            if auto.get("days", "all") == "workdays" and datetime.fromisoformat(row["when"]).weekday() >= 5:
                continue
            row.pop("context_user_id", None)
            row.pop("context_service", None)
            row.update({"context_event_type": "automation_triggered", "context_domain": "automation",
                        "context_entity_id": auto["ref"], "context_name": auto["ref"]})  # fmt: skip
            n += 1
    return n


def _clock_at(day: datetime, hhmm: str, sd_min: float, rng: random.Random) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return day + timedelta(hours=h, minutes=m + (rng.gauss(0, sd_min / 2) if sd_min else 0))


# --- the owner --------------------------------------------------------------------------------------
class Owner:
    """Decides drafts like a person who knows their own habits; every hour of house time."""

    def __init__(
        self, app: polygon.App, habit_entities: set[str], known: set[str], approve: str = "habits"
    ) -> None:
        """`approve`: `habits` (drafts acting only on the story's habits) or `none` (says no to all)."""
        self.app, self.habits, self.known, self.approve = app, habit_entities, known, approve
        self.decided: dict[str, str] = {}

    def __call__(self, ts: float) -> bool:
        if ts % 3600:
            return False
        acted = False
        for p in self.app.get("/api/proposals", status="draft"):
            if p["id"] in self.decided:
                continue
            text = p.get("yaml") or ""
            named = invariants.entities_in(text)
            targets = {e for e in named if invariants._acted_on(text, e)}
            if self.approve == "habits" and targets and targets <= self.habits and named <= self.known:
                r = self.app.send("POST", f"/api/proposals/{p['id']}/approve", {})
                self.decided[p["id"]] = (
                    "approved" if r.status_code == 200 else f"approve failed {r.status_code}"
                )
            else:
                why = (
                    "names something my house does not have"
                    if not named <= self.known
                    else "not something I do"
                    if self.approve == "habits"
                    else "I do not want this automated"
                )
                self.app.send("POST", f"/api/proposals/{p['id']}/reject", {"note": why})
                self.decided[p["id"]] = "rejected"
            acted = True
        return acted


# --- then: expectations -----------------------------------------------------------------------------
def then(story: dict, result: dict, names: dict, has_model: bool) -> list[dict]:
    out = []
    start = result["plan_start"]
    reports = [e for e in result["feed"] if e.get("kind") == "report"]
    proposals = result["proposals"]
    events = result["events"]

    def name(pattern: str) -> str | None:
        return names.get(pattern) or resolve(pattern, result["entities"])

    def targets(p: dict) -> set[str]:
        text = p.get("yaml") or ""
        return {e for e in invariants.entities_in(text) if invariants._acted_on(text, e)}

    def hm(ts: float | None) -> str | None:
        return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else None

    def add(exp: dict, ok: bool | None, detail: str) -> None:
        out.append({"expect": exp, "ok": ok, "detail": detail})

    for exp in story.get("expect") or []:
        kind = next(iter(exp))
        if (
            story.get("needs_model")
            and not has_model
            and kind
            in (
                "proposal_for",
                "deployed_for",
                "max_proposals_for",
                "schedule_fits",
                "owner_part_not_repeated",
            )
        ):
            add(exp, None, "n/a with the fake models (they write no drafts)")
            continue
        if kind == "proposal_for":
            eid, by = name(exp["proposal_for"]), start + exp.get("by_day", story["days"]) * 86400
            hits = [p for p in proposals if eid in targets(p) and (p.get("created") or 0) <= by]
            add(
                exp,
                bool(hits),
                f"{len(hits)} proposal(s) for {eid} by day {exp.get('by_day')}"
                + (f", first at {hm(min(p['created'] for p in hits))}" if hits else ""),
            )
        elif kind == "deployed_for":
            eid = name(exp["deployed_for"])
            hits = [p for p in proposals if eid in targets(p) and p.get("status") == "deployed"]
            add(exp, bool(hits), f"{len(hits)} deployed for {eid}")
        elif kind == "schedule_fits":
            eid = name(exp["schedule_fits"])
            habit = next((h for h in story.get("habits") or [] if name(h["entity"]) == eid), None)
            hits = [p for p in proposals if eid in targets(p) and p.get("status") == "deployed"]
            if habit is None or not hits:
                add(exp, None if habit else False, "no deployed automation" if habit else "no such habit")
                continue
            misfits = [
                f"{p['id']}: {why}" for p in hits if (why := schedule_misfit(p.get("yaml") or "", habit))
            ]
            add(exp, not misfits, "; ".join(misfits) or f"{len(hits)} deployed, schedule fits the habit")
        elif kind == "owner_part_not_repeated":
            auto = next(a for a in story["owner_automations"] if a["ref"] == exp["owner_part_not_repeated"])
            eid = name(auto["entity"])
            done = [e for e in events if e["entity_id"] == eid and e["tag"] == "owner_automation"]
            if not done:
                add(exp, None, f"{auto['ref']} never ran")
                continue
            at = _circular_minute([_minute_of(e["ts"]) for e in done])
            span = auto.get("days", "all")
            repeats = [
                f"{p['id']} ({days})" for p in proposals if eid in targets(p)
                if (days := repeated_days(p.get("yaml") or "", eid, auto["state"], at, span))
            ]  # fmt: skip
            when = f"{auto['state']} about {at // 60:02d}:{at % 60:02d}"
            add(
                exp,
                not repeats,
                f"repeat {auto['ref']} ({when}): {'; '.join(repeats)}"
                if repeats
                else f"no proposal repeats {auto['ref']}",
            )
        elif kind == "max_proposals_for":
            eid = name(exp["max_proposals_for"])
            n = sum(1 for p in proposals if eid in targets(p))
            add(exp, n <= exp["count"], f"{n} proposals for {eid} (max {exp['count']})")
        elif kind == "report_about":
            eid = name(exp["report_about"])
            anchors = [
                e
                for e in events
                # an incident's own state (a fridge going "off" too), never its end
                if (exp.get("after_tag") and e["tag"] == exp["after_tag"] and e["note"] != "back")
                or (exp.get("after_note") and e["note"] == exp["after_note"] and e["state"] != "off")
            ]
            anchors = _first_per_hour(anchors)
            floor = SEVERITY_RANK[exp.get("severity", "info")]
            got = [
                a
                for a in anchors
                if any(
                    about(r, eid) and SEVERITY_RANK.get(_severity(r), 0) >= floor
                    and 0 <= r["ts"] - a["ts"] <= 60 * exp["within_min"]
                    for r in reports
                )  # fmt: skip
            ]
            need = exp.get("share", 1.0)
            ok = bool(anchors) and len(got) >= need * len(anchors)
            misses = [hm(a["ts"]) for a in anchors if a not in got][:5]
            add(
                exp,
                ok if anchors else None,
                f"{len(got)}/{len(anchors)} reported about {eid}"
                + (f" as {exp['severity']} or worse" if exp.get("severity") else "")
                + f" within {exp['within_min']} min"
                + (f"; missed at {misses}" if misses else ""),
            )
        elif kind == "max_alarms_about":
            eid = name(exp["max_alarms_about"])
            n = sum(1 for r in reports if _severity(r) == "alarm" and mentions(r, eid))
            items = [i for i in result["inbox"] if i.get("severity") == "alarm" and mentions(i, eid)]
            add(
                exp,
                len(items) <= exp["count"],
                f"{len(items)} open alarm(s) in the inbox about {eid} ({n} reports)",
            )
        elif kind == "no_alarm_after_tag":
            bad = []
            for e in (e for e in events if e["tag"] == exp["no_alarm_after_tag"]):
                for r in reports:
                    if (
                        _severity(r) == "alarm"
                        and mentions(r, e["entity_id"])
                        and 0 <= r["ts"] - e["ts"] <= 60 * exp["within_min"]
                    ):
                        bad.append(hm(r["ts"]))
            add(
                exp,
                not bad,
                f"{len(bad)} alarm(s) after {exp['no_alarm_after_tag']}" + (f" at {bad[:5]}" if bad else ""),
            )
        elif kind == "no_proposal_from_tag":
            hits, judged = _built_on_lies(proposals, events, exp["no_proposal_from_tag"])
            add(
                exp,
                not hits if judged else None,
                f"{len(hits)} of {judged} proposal(s) on a lying sensor, timed in hours when it mostly lied",
            )
        elif kind == "max_reports_per_day":
            per_day = Counter(
                datetime.fromtimestamp(r["ts"]).date() for r in reports if _severity(r) != "info"
            )
            worst = max(per_day.values(), default=0)
            add(
                exp,
                worst <= exp[kind],
                f"at most {worst} warnings/alarms a day (max {exp[kind]}); "
                f"{sum(1 for r in reports if _severity(r) == 'info')} info reports in all",
            )
        elif kind == "max_event_wakes_per_day":
            wakes = sum(1 for r in result["runs"] if r.get("wake_source") == "event")
            per_day = wakes / max(result["house_days"], 1)
            add(exp, per_day <= exp[kind], f"{per_day:.0f} event wakes a day (max {exp[kind]})")
        elif kind == "never_actuated":
            eid = name(exp[kind])
            acted = [f"{hm(a['ts'])} {a['service']} ({a['verdict']})" for a in result["actions"]
                     if eid in (a.get("entity_ids") or []) and a.get("verdict") != "denied"]  # fmt: skip
            deployed = [p["id"] for p in proposals if eid in targets(p) and p.get("status") == "deployed"]
            add(
                exp,
                not acted and not deployed,
                f"{eid}: {len(acted)} action(s) not denied {acted[:3]}, "
                f"{len(deployed)} deployed automation(s) {deployed[:3]}",
            )
        elif kind == "house_mode":
            anchors = _first_per_hour([
                e for e in events
                if e["note"] == exp.get("after_note")
                or (exp.get("after_tag") and e["tag"] == exp["after_tag"])
            ])  # fmt: skip
            sets = [
                a for a in result["actions"]
                if MODE_HELPER in (a.get("entity_ids") or []) and a.get("verdict") != "denied"
                and (a.get("data") or {}).get("option") == exp[kind]
            ]  # fmt: skip
            got = [
                a for a in anchors if any(0 <= m["ts"] - a["ts"] <= 60 * exp["within_min"] for m in sets)
            ]
            add(
                exp,
                len(got) == len(anchors) if anchors else None,
                f"mode {exp[kind]} within {exp['within_min']} min after {len(got)}/{len(anchors)} "
                f"'{exp.get('after_note') or exp.get('after_tag')}'",
            )
        elif kind == "max_manager_runs_per_day":
            per_day = Counter(datetime.fromtimestamp(r["started"]).date() for r in result["runs"]
                              if r.get("wake_source") == "manage")  # fmt: skip
            worst = max(per_day.values(), default=0)
            add(exp, worst <= exp[kind], f"at most {worst} manager runs a day (max {exp[kind]})")
        elif kind == "max_fault_wakes":
            n = sum(1 for e in events if e["tag"] == exp[kind])
            woke = _fault_wakes(result).get(exp[kind], 0)
            share = woke / n if n else 0.0
            add(
                exp,
                share <= exp["share"] if n else None,
                f"{woke}/{n} {exp[kind]} events woke an agent ({share:.0%}, max {exp['share']:.0%})",
            )
        elif kind == "quiet_while_tag":
            bad = []
            for on, off in _windows(events, exp[kind]):
                bad += [f"run {hm(r['started'])} {r.get('agent_id')}" for r in result["runs"]
                        if on < r["started"] < off]  # fmt: skip
                bad += [
                    f"action {hm(a['ts'])} {a.get('entity_ids')} ({a['verdict']})" for a in result["actions"]
                    if on < a["ts"] < off and a.get("verdict") != "denied"
                ]  # fmt: skip
            windows = _windows(events, exp[kind])
            add(
                exp,
                not bad if windows else None,
                f"{len(bad)} run(s)/action(s) in {len(windows)} window(s) of {exp[kind]}"
                + (f": {bad[:4]}" if bad else ""),
            )
        else:
            add(exp, False, f"unknown expectation {kind}")
    return out


ALL_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def schedule_misfit(text: str, habit: dict, band_min: int = 45) -> str | None:
    """Why an automation's schedule does not do the habit (None: it does). Time triggers within
    `band_min` of the habit's on and off times; the top-level weekday condition covers exactly the
    habit's days (no condition = every day). Stories run 2026-11-14 #2: Mon–Fri drafted as Mon–Wed."""
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as err:
        return f"yaml: {err}"
    doc = doc[0] if isinstance(doc, list) and doc else doc
    triggers = doc.get("triggers") or doc.get("trigger") or []
    times = []
    for t in triggers if isinstance(triggers, list) else [triggers]:
        if isinstance(t, dict) and (t.get("trigger") or t.get("platform")) == "time":
            for at in t["at"] if isinstance(t.get("at"), list) else [t.get("at")]:
                if isinstance(at, str) and re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", at):
                    h, m = at.split(":")[:2]
                    times.append(int(h) * 60 + int(m))
    problems = []
    for key in ("on", "off"):
        h, m = (int(x) for x in habit[key].split(":"))
        if not any(abs((t - h * 60 - m + 720) % 1440 - 720) <= band_min for t in times):
            problems.append(f"no time trigger near {habit[key]} ({key})")
    conditions = doc.get("conditions") or doc.get("condition") or []
    days = set(ALL_DAYS)
    for c in conditions if isinstance(conditions, list) else [conditions]:
        if isinstance(c, dict) and c.get("condition") == "time" and c.get("weekday"):
            wd = c["weekday"]
            days &= set(wd if isinstance(wd, list) else [wd])
    want = set(ALL_DAYS[:5]) if habit.get("days", "all") == "workdays" else set(ALL_DAYS)
    if days != want:
        order = lambda ds: ",".join(d for d in ALL_DAYS if d in ds)  # noqa: E731
        problems.append(f"runs on {order(days) or 'no day'}, habit is {order(want)}")
    return "; ".join(problems) or None


def _minute_of(ts: float) -> int:
    moment = datetime.fromtimestamp(ts)
    return moment.hour * 60 + moment.minute


def _circular_minute(minutes: list[int]) -> int:
    anchor = minutes[0]
    offsets = sorted((m - anchor + 720) % 1440 - 720 for m in minutes)
    return int(anchor + offsets[len(offsets) // 2]) % 1440


def _weekdays(conditions) -> set[str]:
    days = set(ALL_DAYS)
    for c in conditions if isinstance(conditions, list) else [conditions]:
        if isinstance(c, dict) and c.get("condition") == "time" and c.get("weekday"):
            wd = c["weekday"]
            days &= set(wd if isinstance(wd, list) else [wd])
    return days


def repeated_days(text: str, eid: str, state: str, at: int, days: str, band_min: int = 45) -> str:
    """The automation's days on which it sets `eid` to `state` from a time trigger near `at`
    that the owner's automation already covers ("" = none). Weekday conditions count at the top
    and in the `choose` option that holds the action."""
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return ""
    doc = doc[0] if isinstance(doc, list) and doc else doc
    triggers = doc.get("triggers") or doc.get("trigger") or []
    near = False
    for t in triggers if isinstance(triggers, list) else [triggers]:
        if isinstance(t, dict) and (t.get("trigger") or t.get("platform")) == "time":
            for value in t["at"] if isinstance(t.get("at"), list) else [t.get("at")]:
                if isinstance(value, str) and re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", value):
                    h, m = (int(x) for x in value.split(":")[:2])
                    near |= abs((h * 60 + m - at + 720) % 1440 - 720) <= band_min
    if not near:
        return ""
    top = _weekdays(doc.get("conditions") or doc.get("condition") or [])
    service = {"off": "turn_off", "on": "turn_on"}.get(state, state)
    hit: set[str] = set()

    def walk(steps, days: set[str]) -> None:
        nonlocal hit
        for step in steps if isinstance(steps, list) else [steps]:
            if not isinstance(step, dict):
                continue
            for option in step.get("choose") or []:
                walk(option.get("sequence") or [], days & _weekdays(option.get("conditions") or []))
            walk(step.get("default") or [], days)
            action = str(step.get("action") or step.get("service") or "")
            if action.endswith(service) and eid in json.dumps(step):
                hit |= days

    walk(doc.get("actions") or doc.get("action") or [], top)
    covered = set(ALL_DAYS[:5]) if days == "workdays" else set(ALL_DAYS)
    return ",".join(d for d in ALL_DAYS if d in hit & covered)


def mentions(row: dict, eid: str | None) -> bool:
    """The report names exactly `eid` (not a longer id that starts with it)."""
    return (
        bool(eid) and re.search(rf"(?<![\w.]){re.escape(eid)}(?![\w])", row.get("summary") or "") is not None
    )


def about(row: dict, eid: str | None) -> bool:
    """A report about `eid`: woken by its change (the feed links it), or naming it. Models often
    write "Bedroom window" rather than the id (stories 2026-11-15: two warnings counted as missed)."""
    return bool(eid) and (row.get("entity_id") == eid or mentions(row, eid))


def _built_on_lies(proposals: list[dict], events: list[dict], tag: str) -> tuple[list[str], int]:
    """Drafts triggered by a sensor that lied (`tag`), whose times fall in hours of the day when
    most of that sensor's events were lies (ghost motion at night): a habit learnt from noise."""
    tagged = [e["ts"] for e in events if e["tag"] == tag]
    if not tagged:
        return [], 0
    # a draft made before the first such event cannot have learnt from it (the first look at day 0
    # drafts from the history before the story)
    proposals = [p for p in proposals if (p.get("created") or 0) > min(tagged)]
    lies, total = Counter(), Counter()
    for e in events:
        hour = datetime.fromtimestamp(e["ts"]).hour
        total[(e["entity_id"], hour)] += 1
        lies[(e["entity_id"], hour)] += e["tag"] == tag
    liars = {eid for eid, _ in lies if lies[(eid, _)]}
    hits, judged = [], 0
    for p in proposals:
        text = p.get("yaml") or ""
        on = invariants.entities_in(text) & liars
        hours = {int(h) for h in re.findall(r"\b([01]?\d|2[0-3]):[0-5]\d", text)}
        if not on:
            continue
        judged += 1
        if any(total[(e, h)] and lies[(e, h)] / total[(e, h)] >= 0.5 for e in on for h in hours):
            hits.append(p["id"])
    return hits, judged


def _severity(row: dict) -> str:
    return ((row.get("data") or {}).get("severity")) or "info"


SEVERITY_RANK = {"info": 0, "warning": 1, "alarm": 2}
MODE_HELPER = "input_select.agent_house_mode"


def _windows(events: list[dict], tag: str) -> list[tuple[float, float]]:
    """(on, off) spans of an injected switch, e.g. the kill switch helper held on for an hour."""
    out, start = [], None
    for e in sorted((e for e in events if e["tag"] == tag), key=lambda e: e["ts"]):
        if e["state"] == "on" and start is None:
            start = e["ts"]
        elif e["state"] != "on" and start is not None:
            out.append((start, e["ts"]))
            start = None
    return out


def _first_per_hour(anchors: list[dict]) -> list[dict]:
    out, last = [], -1e18
    for a in sorted(anchors, key=lambda a: a["ts"]):
        if a["ts"] - last > 3600:
            out.append(a)
            last = a["ts"]
    return out


# --- coverage ---------------------------------------------------------------------------------------
FEATURES = (
    "daily_review",
    "first_look",
    "proposal_drafted",
    "proposal_blocked",
    "proposal_deployed",
    "proposal_rejected",
    "manager_run",
    "alarm_report",
    "warning_report",
    "gate_drop_cap",
    "gate_drop_cooldown",
    "house_mode_action",
    "room_run",
    "orchestrator_poke",
)


def coverage(result: dict) -> dict[str, int]:
    runs, feed, props = result["runs"], result["feed"], result["proposals"]
    c = Counter()
    for r in runs:
        if r.get("wake_source") in ("manager", "manage"):  # the manager runs as the orchestrator
            c["manager_run"] += 1
        elif r.get("agent_id") == "orchestrator":
            c["daily_review" if r.get("wake_source") == "review" else "orchestrator_poke"] += 1
        else:
            c["room_run"] += 1
    for p in props:
        c["proposal_drafted"] += 1
        c[f"proposal_{p.get('status')}"] += 1 if p.get("status") in ("blocked", "deployed", "rejected") else 0
    for e in feed:
        text = e.get("summary") or e.get("text") or ""
        if e.get("kind") == "gate_drop":
            c[
                "gate_drop_cap"
                if "cap" in text
                else "gate_drop_cooldown"
                if "cooldown" in text
                else "gate_drop"
            ] += 1
        if e.get("kind") == "report":
            c[f"{_severity(e)}_report"] += 1
        if "first look" in text.lower():
            c["first_look"] += 1
    c["house_mode_action"] += sum(
        1 for a in result["actions"] if MODE_HELPER in (a.get("entity_ids") or [])
    )
    tags = Counter(e["tag"] for e in result["events"])
    woke = _fault_wakes(result)
    for tag in tags:
        if tag.startswith("fault:"):
            c[f"{tag} (woke an agent)"] = woke.get(tag, 0)
    return dict(c)


def _fault_wakes(result: dict) -> dict[str, int]:
    """How often a sensor lie woke an agent within 2 min (noise the prefilter let through)."""
    out: dict[str, int] = defaultdict(int)
    starts = sorted(r["started"] for r in result["runs"] if r.get("wake_source") == "event")
    for e in result["events"]:
        if e["tag"].startswith("fault:") and any(0 <= s - e["ts"] <= 120 for s in starts):
            out[e["tag"]] += 1
    return out


# --- running ----------------------------------------------------------------------------------------
def run_story(args, sid: str, story: dict, seed: int, out_dir: Path) -> dict:
    spec, cfg, checks, _ = polygon._prepare(args)
    days_mod.quiet_addon_loops()
    stack, app, meta = polygon._start(args, spec, cfg, checks, out_dir / f"{sid}-{seed}" / "data")
    has_model = cfg.anthropic.backend != "fake" or cfg.openai.backend != "fake"
    try:
        if story.get("settings"):
            r = app.send("PUT", "/api/settings", story["settings"])
            if r.status_code >= 300:
                raise polygon.SetupError(f"settings: {r.status_code} {r.text[:200]}")
        if story.get("mode_helper"):  # what the owner does first: Settings → House mode → create helper
            r = app.send("POST", "/api/council/mode-helper", {})
            if r.status_code >= 300:
                raise polygon.SetupError(f"mode helper: {r.status_code} {r.text[:200]}")
        for room in story.get("suggested_agents") or []:  # the owner keeps the editor's suggestions
            accept_suggested_rules(app, room)
        for h in story.get("helpers") or []:  # e.g. the kill switch input_boolean the story flips
            attrs = {"friendly_name": h.get("name", h["entity"])}
            stack.on_ha(lambda h=h, a=attrs: stack.ha.set_state(h["entity"], h.get("state", "off"), a))
        entities = stack.on_ha(lambda: sorted(stack.ha.states))
        stack.on_ha(lambda: owner_history(story, stack.ha, entities))
        start = days_mod.first_midnight()
        events, names = given(story, start, seed, entities)
        missing = [k for k, v in names.items() if v is None]
        if missing:
            return {"story": sid, "seed": seed, "ok": None, "skipped": f"the house has no {missing}"}
        habit_ids = {names[h["entity"]] for h in story.get("habits") or []}
        owner = Owner(app, habit_ids, set(entities), (story.get("owner") or {}).get("approve", "habits"))
        result = days_mod.play_days(stack, events, start.timestamp(), story["days"], on_checkpoint=owner)
    finally:
        stack.stop()
    result["plan_start"] = start.timestamp()
    safety = polygon.check(result, meta["mode"], {}) + invariants.check_trace(
        result, meta["mode"], result.get("caps")
    )
    expects = then(story, result, names, has_model)
    ok_values = [e["ok"] for e in expects if e["ok"] is not None] + [c["ok"] for c in safety]
    return {
        "story": sid,
        "seed": seed,
        "ok": all(ok_values),
        "expect": expects,
        "failed_checks": [c for c in safety if not c["ok"]],
        "coverage": coverage(result),
        "owner": owner.decided,
        "house_days": result["house_days"],
        "duration_s": result["duration_s"],
        "runs": len(result["runs"]),
        "proposals": len(result["proposals"]),
        "data_dir": str(out_dir / f"{sid}-{seed}" / "data"),
    }


def accept_suggested_rules(app: polygon.App, room: str) -> None:
    """What the owner gets by keeping the editor's suggested wake rules (GET api/agents/suggest-rules)
    for `room`: the room's agent takes exactly those rules; a room without an agent gets one."""
    rules = app.get("/api/agents/suggest-rules", room=room)["rules"]
    agent = next((a for a in app.get("/api/agents") if a["room"] == room), None)
    if agent is None:
        r = app.send("POST", "/api/agents", {"room": room, "wake_rules": {"events": rules}})
    else:
        config = app.get(f"/api/agents/{agent['id']}")
        config["wake_rules"] = {**(config.get("wake_rules") or {}), "events": rules}
        r = app.send("PUT", f"/api/agents/{agent['id']}", config)
    if r.status_code >= 300:
        raise polygon.SetupError(f"suggested rules for {room}: {r.status_code} {r.text[:200]}")


def cmd_run(args) -> int:
    stories = load()
    chosen = list(stories) if args.story == ["all"] else args.story
    if unknown := [s for s in chosen if s not in stories]:
        raise SystemExit(f"unknown stories {unknown}; see `python -m sim.stories list`")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = args.out_dir or polygon.ROOT / ".agent-logs" / "stories" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    args.out_dir = None  # each run gets its own data dir below
    report: dict = {"stories": {}, "coverage": {}}
    total = Counter()
    for sid in chosen:
        runs = []
        for i in range(args.k):
            print(f"stories: {sid} seed {args.seed + i} …", file=sys.stderr, flush=True)
            res = run_story(args, sid, stories[sid], args.seed + i, out_dir)
            runs.append(res)
            for e in res.get("expect") or []:
                mark = {True: "ok  ", False: "FAIL", None: "n/a "}[e["ok"]]
                print(f"  {mark} {next(iter(e['expect']))}: {e['detail']}", file=sys.stderr)
            for c in res.get("failed_checks") or []:
                print(f"  FAIL {c['name']}: {c['items'][:3]}", file=sys.stderr)
            total.update({k: v for k, v in (res.get("coverage") or {}).items()})
        judged = [r["ok"] for r in runs if r["ok"] is not None]
        full = len(judged) == len(runs)  # pass^k only over k judged runs; a skipped run leaves it open
        report["stories"][sid] = {
            "about": stories[sid].get("about"),
            "k": args.k,
            "skipped": len(runs) - len(judged),
            "pass@1": round(sum(judged) / len(judged), 2) if judged else None,
            "pass^k": (all(judged) if full else (False if not all(judged) else None)) if judged else None,
            "runs": runs,
        }
    report["coverage"] = {f: total.get(f, 0) for f in FEATURES} | {
        k: v for k, v in total.items() if k not in FEATURES
    }
    report["never_reached"] = [f for f in FEATURES if not total.get(f)]
    path = out_dir / "report.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    (out_dir / "report.md").write_text(render(report))
    print(render(report), file=sys.stderr)
    failed = [s for s, r in report["stories"].items() if r["pass^k"] is False]
    print(json.dumps({"ok": not failed, "report": str(path), "failed": failed}))
    return 0 if not failed else 1


def render(report: dict) -> str:
    lines = ["| story | pass@1 | pass^k | k |", "|---|---|---|---|"]
    for sid, r in report["stories"].items():
        lines.append(f"| {sid} | {r['pass@1']} | {r['pass^k']} | {r['k']} |")
    lines += ["", "Coverage (hits over all runs):", ""]
    lines += [f"- {k}: {v}" for k, v in report["coverage"].items()]
    lines += ["", f"Never reached: {', '.join(report['never_reached']) or 'none'}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.stories", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="the stories")
    run = sub.add_parser("run", help="live stories k times each, check, report pass^k and coverage")
    run.add_argument("--story", nargs="+", default=["all"])
    run.add_argument("--k", type=int, default=2, help="runs per story, seeds seed..seed+k-1")
    run.add_argument("--seed", type=int, default=7)
    polygon._common(run)
    run.set_defaults(rate=0.0, backfill_days=14)
    args = ap.parse_args(argv)
    if args.cmd == "list":
        for sid, s in load().items():
            print(f"{sid:24} {s['days']:>2} d  {s.get('about', '')}")
        return 0
    try:
        return cmd_run(args)
    except polygon.SetupError as err:
        print(f"stories: setup failed: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
