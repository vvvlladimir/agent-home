"""Live N days of a household in minutes: discrete-event time for the whole polygon.

The add-on, the fake HA and the in-process learner share one house clock (agent_house/app/core/
clock.py). This player walks a household plan (sim/household.py) in time order. For every event it
jumps the clock to the event's time, sets the state in the fake HA, waits until the add-on has seen
it, and waits until every model call and background job is idle. Every `CHECKPOINT_MIN` of house time
it ticks the council itself (daily review, house manager, house mode, unusual notes) and the learner
link (forward, fetch the house model), the work their real-time loops would have done in the gap; at
`TRAIN_AT` house time the learner trains, as the Jetson does at night.

Real work keeps real time: a model call takes as long as it takes, and the house waits for it. With
the fake models a month takes a few minutes; with real ones, as long as their calls.

    python -m sim.polygon days --days 30 --seed 7          # fakes, shadow, report + checks
    python -m sim.polygon days --days 7 --chaos calm       # a tidy household, for comparison
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from sim import clock
from sim.household import Event, summary

CHECKPOINT_MIN = 15  # house minutes between council ticks
TRAIN_AT = (3, 30)  # the learner's nightly training (jetson/learner/server.py AH_TRAIN_AT)
SETTLE_TIMEOUT_S = 600.0  # real seconds one step may take before the run counts as stuck


class Driver:
    """Reach into the in-process add-on: its loop, council, orchestrator and learner link (simulation
    only), and the in-process learner itself."""

    def __init__(self, stack) -> None:
        if stack.addon is None:
            raise RuntimeError("days needs the add-on served in-process (boot(serve_addon=True))")
        self.stack, self.ctx = stack, stack.addon.state.ctx
        task = getattr(self.ctx.council, "_task", None)
        if task is None:
            raise RuntimeError("the add-on has not started yet")
        self.loop = task.get_loop()

    def call(self, fn, timeout: float = 30):
        async def run():
            return fn()

        return asyncio.run_coroutine_threadsafe(run(), self.loop).result(timeout)

    def acall(self, make_coro, timeout: float = SETTLE_TIMEOUT_S):
        async def run():
            return await make_coro()

        return asyncio.run_coroutine_threadsafe(run(), self.loop).result(timeout)

    def apply(self, ev: Event) -> bool:
        ha = self.stack.ha
        if ev.entity_id.startswith("event:"):  # an HA event, e.g. the owner tapping an ask's button
            kind = ev.entity_id.split(":", 1)[1]
            self.stack.on_ha(lambda: ha.fire_event(kind, {"action": ev.state}))
            return True
        if ev.entity_id not in ha.states:
            return False
        if ev.cause.startswith("automation:"):  # the owner's own automation (stories: owner_automations)
            ref = ev.cause.split(":", 1)[1]
            self.stack.on_ha(lambda: ha.run_automation(ref, [(ev.entity_id, ev.state)]))
            return True
        change = ha.user_change if ev.cause == "user" else ha.physical_change
        self.stack.on_ha(lambda: change(ev.entity_id, ev.state))
        return True

    def seen(self, ev: Event, timeout: float = 5) -> bool:
        """The add-on's mirror of HA has the new state (the WebSocket event arrived)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            mirror = self.call(lambda: (self.ctx.ha.states.get(ev.entity_id) or {}).get("state"))
            if mirror == ev.state:
                return True
            time.sleep(0.01)
        return False

    def busy(self) -> bool:
        def check() -> bool:
            orch = self.ctx.orchestrator
            review = getattr(self.ctx.council, "review", None)
            manager = getattr(self.ctx.council, "manager", None)
            task = getattr(manager, "_task", None)  # without it a trip passed during one model call
            return bool(
                getattr(orch, "_tasks", None)
                or (review is not None and review.running)
                or (task is not None and not task.done())
            )

        return self.call(check)

    def settle(self, timeout: float = SETTLE_TIMEOUT_S) -> bool:
        """Wait until nothing runs; False on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.busy():
                return True
            time.sleep(0.02)
        return False

    def checkpoint(self) -> None:
        self.acall(lambda: self.ctx.learner.tick())  # forward, registry, the house model
        self.acall(lambda: self.ctx.council.tick(clock.local()))

    def train(self) -> dict | None:
        """The learner's nightly training, now (house time), in this thread; None without one."""
        learner = getattr(self.stack, "learner", None)
        if learner is None or self.stack.cfg.jetson.learner != "sim" or not learner.journal.stats()["events"]:
            return None
        link = self.ctx.learner

        async def drain() -> None:  # what happened up to now is in the journal first
            for _ in range(100):
                left = len(link.buffer)
                await link.push()
                if not link.buffer or len(link.buffer) >= left:
                    return

        self.acall(drain)
        try:
            return learner.train()
        except Exception:  # noqa: BLE001 - recorded in learner.training; the house goes on
            return None


def quiet_addon_loops() -> None:
    """Before the add-on boots: its council loop would tick on real time next to the player's
    checkpoints and start a review twice. In-process only; the player ticks the council itself."""
    from app.core import council  # noqa: PLC0415

    council.TICK_S = 10**9


def first_midnight() -> object:
    """The next local midnight in house time: day 0 of the plan."""
    now = clock.local()
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def play_days(
    stack,
    events: list[Event],
    start: float,
    days: int,
    progress=None,
    on_checkpoint=None,
    snapshots: bool = True,
) -> dict:
    """Play `events` (sorted) from house time `start` (a local midnight) for `days` days, and on to
    the next morning so the last day's review runs; return what happened, as `polygon.check` reads.
    `on_checkpoint(ts)` runs after each settled checkpoint: the simulated owner (sim/stories.py)."""
    drv = Driver(stack)
    ha = stack.ha
    t0 = clock.time()
    calls0 = stack.on_ha(lambda: len(ha.service_calls))
    writes0 = stack.on_ha(lambda: len(ha.config_writes))
    gw = stack.sim.gw.log
    seq0 = gw.total
    owners_before = stack.on_ha(lambda: _owner_automations(ha))
    wall0, applied, skipped, unseen, stuck = time.monotonic(), 0, 0, 0, []
    step = CHECKPOINT_MIN * 60
    clock.travel_to(start)  # straight to day 0: no half day before the plan
    next_check = start
    until = start + days * 86400 + 6 * 3600  # the morning after: the last day's review at 04:30
    last_day = None
    taken: list[str] = []
    feed: dict[int, dict] = {}  # the add-on keeps the last 1000 feed rows; a month has more

    def keep_feed() -> None:
        for row in drv.call(lambda: drv.ctx.feed.recent(limit=1000)):
            if row.get("ts", 0) >= t0:
                feed.setdefault(row["id"], row)

    trainings, live_entities = 0, set()

    def train() -> int:
        doc = drv.train()
        live_entities.update(m["entity_id"] for m in (doc or {}).get("live") or [])
        return doc is not None

    for ev in events:
        while next_check <= ev.ts:  # the work the real-time loops would have done meanwhile
            clock.travel_to(next_check)
            if _is_training_time(next_check):
                trainings += train()
            drv.checkpoint()
            if not drv.settle():
                stuck.append({"ts": next_check, "at": "checkpoint"})
            if on_checkpoint is not None and on_checkpoint(next_check):
                drv.settle()
            if next_check % 3600 == 0:
                keep_feed()
            if snapshots and _is_midnight(next_check):  # a day to restore from (sim/snapshot.py)
                taken.append(stack.sim.take_snapshot(f"day {clock.local().date()}")["snapshot"])
            next_check += step
        clock.travel_to(ev.ts)
        if not drv.apply(ev):
            skipped += 1
            continue
        applied += 1
        if not drv.seen(ev):
            unseen += 1
        if not drv.settle():
            stuck.append({"ts": ev.ts, "at": ev.entity_id, "tag": ev.tag})
        day = clock.local().date()
        if progress and day != last_day:
            progress(day, applied)
            last_day = day
    while next_check <= until:
        clock.travel_to(next_check)
        if _is_training_time(next_check):
            trainings += train()
        drv.checkpoint()
        if not drv.settle():
            stuck.append({"ts": next_check, "at": "checkpoint"})
        if on_checkpoint is not None and on_checkpoint(next_check):
            drv.settle()
        next_check += step
    drv.settle()
    keep_feed()
    t1 = clock.time()

    ctx = drv.ctx
    runs = drv.call(lambda: _all_runs(ctx, t0))
    run_ids = {r["id"] for r in runs}
    actions = [a for a in drv.call(lambda: _all_actions(ctx)) if a["ts"] >= t0 or a.get("run_id") in run_ids]
    return {
        "scenario": "days",
        "description": f"{len(events)} household events over {(t1 - t0) / 86400:.1f} house days",
        "plan": summary(events),
        "events": [_row(e) for e in events],
        "started": t0,
        "ended": t1,
        "house_days": days,
        "duration_s": round(time.monotonic() - wall0, 1),
        "applied": applied,
        "skipped_missing_entity": skipped,
        "unseen_by_addon": unseen,
        "stuck": stuck,
        "snapshots": taken,
        "timed_out": bool(stuck),
        "runs": runs,
        "actions": actions,
        "feed": sorted(feed.values(), key=lambda e: e["id"]),
        "caps": {"manager": drv.call(lambda: ctx.runtime.manager_max_wakes_per_hour)},
        "review_expected": bool(drv.call(lambda: getattr(ctx.runtime, "daily_review_at", "04:30"))),
        "inbox": [i for i in drv.call(lambda: ctx.inbox.list(limit=100_000)) if i.get("ts", 0) >= t0],
        "findings": drv.call(lambda: ctx.insights.list()),
        "last_scan": drv.call(lambda: ctx.store.get("insights_last_scan")),
        "learner": {
            "trainings": trainings,
            "live_entities": sorted(live_entities),  # targets any training of the run offered
            "asks": drv.call(lambda: ((ctx.store.get("learner_model") or {}).get("asks"))),
            "journal": stack.learner.journal.stats() if getattr(stack, "learner", None) else None,
            "link": drv.call(lambda: ctx.learner.status()["link"]),
            "model": drv.call(lambda: (ctx.learner.status()["model"] or {}).get("counts")),
            "live": drv.call(lambda: ctx.learner.status()["live"]),
        },
        "proposals": drv.call(lambda: ctx.lifecycle.list()),
        "ha_calls": stack.on_ha(lambda: [c for c in ha.service_calls[calls0:] if c.get("via") == "api"]),
        "ha_config_writes": stack.on_ha(lambda: list(ha.config_writes[writes0:])),
        "owner_automations_changed": owners_before != stack.on_ha(lambda: _owner_automations(ha)),
        "llm_requests": [r for r in gw.failed if r["seq"] >= seq0],  # the failed ones (the check reads these)
        "llm_requests_total": gw.total - seq0,
        "entities": stack.on_ha(lambda: sorted(ha.states)),
    }


def _is_training_time(ts: float) -> bool:
    t = clock.local().fromtimestamp(ts).astimezone()
    return (t.hour, t.minute) == TRAIN_AT


def _is_midnight(ts: float) -> bool:
    t = clock.local().fromtimestamp(ts).astimezone()
    return t.hour == 0 and t.minute == 0


def _all_runs(ctx, t0: float) -> list[dict]:
    """Every run since `t0`: the API pages by 200, a month has more."""
    out, before = [], None
    while True:
        page = ctx.runs.list(limit=200, before=before)
        out += [r for r in page if r["started"] >= t0]
        if len(page) < 200 or page[-1]["started"] < t0:
            return out
        before = page[-1]["id"]


def _all_actions(ctx) -> list[dict]:
    from app.core.actuator import _row as action_row  # noqa: PLC0415  (the API's own row shape)

    return [action_row(r) for r in ctx.store.query("SELECT * FROM actions ORDER BY id DESC")]


def _row(e: Event) -> dict:
    return {"ts": e.ts, "entity_id": e.entity_id, "state": e.state, "cause": e.cause, "tag": e.tag,
            "note": e.note}  # fmt: skip


def _owner_automations(ha) -> dict:
    import json  # noqa: PLC0415

    ours = "agent_house_"
    return {k: json.dumps(v, sort_keys=True) for k, v in ha.automations.items() if not k.startswith(ours)}
