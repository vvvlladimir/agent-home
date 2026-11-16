"""What a candidate automation would have done on the journal's last days (D38, contracts/schemas/
learner_replay.schema.json). An estimate from what people did by hand, never proof.

Only simple automations replay; anything else is named in `unsupported` and nothing is counted:
- triggers: state (to / from / for), numeric_state (above / below / for), time (at), sun (sunset /
  sunrise with an offset, from `sun.sun` going below / above the horizon);
- conditions: state, numeric_state, time (after / before / weekday), sun (after / before sunset or
  sunrise, read from `sun.sun`), and / or / not;
- actions: service calls that set an entity to a state (turn_on / turn_off, open / close, select_option,
  set_hvac_mode); notifications and delays are ignored.

Outcome of each entity it would have set: right = a person set the same within 10 minutes after;
no_op = it already was; wrong = nobody did, and someone was active (a change by hand anywhere within
half an hour) while the device stayed the other way; unobserved = nobody was active.
"""

from __future__ import annotations

import bisect
from datetime import timedelta

from app.core import predict

RIGHT_S = 600.0
ACTIVE_S = 1800.0
MAX_EXAMPLES = 20
IGNORED_ACTIONS = ("notify.", "persistent_notification.", "logbook.", "system_log.")
SERVICE_STATE = {
    "turn_on": "on", "turn_off": "off", "open_cover": "open", "close_cover": "closed", "lock": "locked",
    "unlock": "unlocked", "open_valve": "open", "close_valve": "closed",
}  # fmt: skip


class Unsupported(Exception):
    pass


def _list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _seconds(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, dict):
        return (
            float(value.get("hours", 0)) * 3600 + float(value.get("minutes", 0)) * 60
            + float(value.get("seconds", 0)) + float(value.get("days", 0)) * 86400
        )  # fmt: skip
    if isinstance(value, str):
        sign = -1 if value.startswith("-") else 1
        parts = [float(p) for p in value.lstrip("+-").split(":")]
        while len(parts) < 3:
            parts.append(0.0)
        return sign * (parts[0] * 3600 + parts[1] * 60 + parts[2])
    raise Unsupported(f"duration {value!r}")


def _hm(value: str) -> float:
    """'HH:MM[:SS]' → seconds after midnight."""
    if not isinstance(value, str) or "." in value:
        raise Unsupported(f"time {value!r} (an entity or template)")
    return _seconds(value)


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Timeline:
    def __init__(self, changes) -> None:
        rows: dict[str, list] = {}
        for c in changes:
            rows.setdefault(c.entity_id, []).append((c.ts, c.state))
        self.lines = {e: predict.Line(r) for e, r in rows.items()}
        self.manual = sorted(c.ts for c in changes if c.manual)
        self.manual_by: dict[tuple[str, str], list[float]] = {}
        for c in changes:
            if c.manual:
                self.manual_by.setdefault((c.entity_id, c.state), []).append(c.ts)

    def at(self, entity_id: str, t: float) -> tuple[str | None, float | None]:
        line = self.lines.get(entity_id)
        return line.at(t) if line else (None, None)

    def changes_of(self, entity_id: str) -> list[tuple[float, str]]:
        line = self.lines.get(entity_id)
        return list(zip(line.ts, line.values, strict=True)) if line else []


# --- triggers ------------------------------------------------------------------------------------------
def trigger_times(trigger: dict, tl: Timeline, start: float, end: float) -> list[float]:
    kind = trigger.get("trigger") or trigger.get("platform")
    hold = _seconds(trigger.get("for"))
    out: list[float] = []
    if kind == "state":
        to, frm = _list(trigger.get("to")), _list(trigger.get("from"))
        for entity_id in _list(trigger.get("entity_id")):
            rows = tl.changes_of(entity_id)
            for i, (t, v) in enumerate(rows):
                prev = rows[i - 1][1] if i else None
                if prev == v or (to and v not in to) or (frm and prev not in frm):
                    continue
                fire = t + hold
                nxt = rows[i + 1][0] if i + 1 < len(rows) else None
                if nxt is not None and nxt < fire:
                    continue  # it changed again before `for` passed
                out.append(fire)
    elif kind == "numeric_state":
        lo, hi = _num(trigger.get("above")), _num(trigger.get("below"))
        if "value_template" in trigger or "attribute" in trigger:
            raise Unsupported("numeric_state on an attribute or template")
        for entity_id in _list(trigger.get("entity_id")):
            rows = tl.changes_of(entity_id)
            inside_before = False
            for i, (t, v) in enumerate(rows):
                x = _num(v)
                inside = x is not None and (lo is None or x > lo) and (hi is None or x < hi)
                if inside and not inside_before:
                    fire = t + hold
                    j = i + 1
                    while j < len(rows) and rows[j][0] < fire:
                        y = _num(rows[j][1])
                        if y is None or (lo is not None and y <= lo) or (hi is not None and y >= hi):
                            break
                        j += 1
                    else:
                        out.append(fire)
                inside_before = inside
    elif kind == "time":
        for at in _list(trigger.get("at")):
            seconds = _hm(at)
            day = predict._local(start).replace(hour=0, minute=0, second=0, microsecond=0)
            while day.timestamp() < end:
                t = (day + timedelta(seconds=seconds)).timestamp()
                if start <= t < end:
                    out.append(t)
                day += timedelta(days=1)
    elif kind == "sun":
        event = trigger.get("event")
        target = {"sunset": "below_horizon", "sunrise": "above_horizon"}.get(event)
        if target is None:
            raise Unsupported(f"sun event {event!r}")
        offset = _seconds(trigger.get("offset"))
        out += [t + offset for t, v in tl.changes_of("sun.sun") if v == target]
    else:
        raise Unsupported(f"trigger {kind!r}")
    return [t for t in out if start <= t < end]


# --- conditions ----------------------------------------------------------------------------------------
def holds(cond, tl: Timeline, t: float) -> bool:
    if not isinstance(cond, dict):
        raise Unsupported("a template condition")
    kind = cond.get("condition")
    if kind in ("and", "or", "not"):
        parts = [holds(c, tl, t) for c in _list(cond.get("conditions"))]
        return all(parts) if kind == "and" else any(parts) if kind == "or" else not any(parts)
    if kind == "state":
        wanted = [str(s) for s in _list(cond.get("state"))]
        hold = _seconds(cond.get("for"))
        if "attribute" in cond:
            raise Unsupported("state condition on an attribute")
        for entity_id in _list(cond.get("entity_id")):
            value, since = tl.at(entity_id, t)
            if value not in wanted or (hold and (since is None or t - since < hold)):
                return False
        return True
    if kind == "numeric_state":
        lo, hi = _num(cond.get("above")), _num(cond.get("below"))
        for entity_id in _list(cond.get("entity_id")):
            x = _num(tl.at(entity_id, t)[0])
            if x is None or (lo is not None and x <= lo) or (hi is not None and x >= hi):
                return False
        return True
    if kind == "time":
        moment = predict._local(t)
        now = moment.hour * 3600 + moment.minute * 60 + moment.second
        weekdays = _list(cond.get("weekday"))
        if weekdays and ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][moment.weekday()] not in weekdays:
            return False
        after = _hm(cond["after"]) if cond.get("after") is not None else None
        before = _hm(cond["before"]) if cond.get("before") is not None else None
        if after is not None and before is not None and after > before:  # over midnight
            return now >= after or now < before
        return (after is None or now >= after) and (before is None or now < before)
    if kind == "sun":
        if cond.get("after_offset") or cond.get("before_offset"):
            raise Unsupported("sun condition with an offset")
        sun = tl.at("sun.sun", t)[0]
        if sun is None:
            raise Unsupported("sun condition without sun.sun in the journal")
        dark = sun == "below_horizon"
        checks = []
        if cond.get("after") == "sunset" or cond.get("before") == "sunrise":
            checks.append(dark)
        if cond.get("after") == "sunrise" or cond.get("before") == "sunset":
            checks.append(not dark)
        if not checks:
            raise Unsupported("sun condition")
        night = cond.get("after") == "sunset" and cond.get("before") == "sunrise"
        return any(checks) if night else all(checks)
    raise Unsupported(f"condition {kind!r}")


# --- actions -------------------------------------------------------------------------------------------
def targets(actions) -> list[tuple[str, str]]:
    out = []
    for step in _list(actions):
        if not isinstance(step, dict):
            raise Unsupported("an action that is not a mapping")
        if "delay" in step or "variables" in step:
            continue
        service = step.get("action") or step.get("service")
        if not isinstance(service, str):
            raise Unsupported(f"action {sorted(step)[:3]}")
        if service.startswith(IGNORED_ACTIONS):
            continue
        domain, _, name = service.partition(".")
        data = step.get("data") or {}
        entities = (
            _list((step.get("target") or {}).get("entity_id"))
            or _list(step.get("entity_id"))
            or _list(data.get("entity_id"))
        )
        if name == "select_option":
            state = data.get("option")
        elif name == "set_hvac_mode":
            state = data.get("hvac_mode")
        else:
            state = SERVICE_STATE.get(name)
        if state is None or not entities:
            raise Unsupported(f"action {service}")
        out += [(e, str(state)) for e in entities if isinstance(e, str)]
    return out


# --- the replay ----------------------------------------------------------------------------------------
def replay(journal, automation: dict, days: int = 30, now: float | None = None) -> dict:
    import time

    now = now or time.time()
    start = now - days * 86400
    unsupported: list[str] = []
    result = {"supported": False, "unsupported": unsupported, "days": days, "fires": 0, "per_day": 0.0,
              "outcomes": {"right": 0, "wrong": 0, "no_op": 0, "unobserved": 0}, "examples": []}  # fmt: skip
    if not isinstance(automation, dict):
        unsupported.append("not an automation")
        return result
    triggers = _list(automation.get("triggers") or automation.get("trigger"))
    conditions = _list(automation.get("conditions") or automation.get("condition"))
    tl = Timeline(journal.changes(start - 86400, now))
    try:
        sets = targets(automation.get("actions") or automation.get("action"))
        moments = sorted({round(t, 3) for trig in triggers for t in trigger_times(trig, tl, start, now)})
        fires = [t for t in moments if all(holds(c, tl, t) for c in conditions)]
    except Unsupported as err:
        unsupported.append(str(err))
        return result
    if not triggers or not sets:
        unsupported.append("no trigger" if not triggers else "no action that sets a device")
        return result
    result["supported"] = True
    last_fire = -1e18
    for t in fires:
        if t - last_fire < 1:
            continue
        last_fire = t
        result["fires"] += 1
        for entity_id, state in sets:
            current = tl.at(entity_id, t)[0]
            if current == state:
                outcome = "no_op"
            else:
                did = tl.manual_by.get((entity_id, state), [])
                j = bisect.bisect_right(did, t)
                if j < len(did) and did[j] <= t + RIGHT_S:
                    outcome = "right"
                else:
                    k = bisect.bisect_left(tl.manual, t - ACTIVE_S)
                    active = k < len(tl.manual) and tl.manual[k] <= t + ACTIVE_S
                    outcome = "wrong" if active else "unobserved"
            result["outcomes"][outcome] += 1
            if len(result["examples"]) < MAX_EXAMPLES:
                example = {"ts": t, "entity_id": entity_id, "state": state, "outcome": outcome}
                result["examples"].append(example)
    result["per_day"] = round(result["fires"] / max(days, 1), 2)
    return result
