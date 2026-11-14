"""The orchestrator as house manager (D27): looks after the house between the daily reviews.

Wakes are deterministic and cheap; the model is only called when one of them finds work
(CLAUDE.md: a prefilter decides whether any LLM is called):

- an escalation that stayed on the board for the orchestrator (no room owner was named);
- the sweep every `manager_sweep_min` minutes, and only if `anomalies()` is not empty:
  nobody home (or night) and lights / media / fans still on; room runs that ended in error or
  escalated; deployed automations of ours whose precision or coverage fell below `metrics.WEAK`;
- the owner or a tester: `POST /api/manager/wake`.
At most `manager_max_wakes_per_hour`, one run at a time, never with the kill switch engaged.

What the manager may do, and through what (nothing reaches HA any other way):
- `call_service` on entities it owns (no area, or an area without a room agent) → the actuator,
  as agent `orchestrator`: policy (tiers, ownership, service allowlist), shadow unless
  `orchestrator_mode: live` and the global mode is live, action log, trace;
- `assign_task` to the room agent that owns an entity → `council.assign_task` (depth 1, hourly cap);
- `propose_automation`: a new automation, or a change to one of ours (`ha_id`) → a draft through
  `orchestrator.draft`: checked, and deployed only when the owner (or a tester) approves it;
- `report` to the owner's inbox.
It runs on the orchestrator's providers (the cloud model with `orchestrator_provider: openai` /
`anthropic`), with the same redaction and daily budget as drafts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from datetime import datetime

from .agents import is_noise
from .council import MODE_ENTITY, ORCHESTRATOR
from .insights import ACT_DOMAINS
from .llm import Priority
from .metrics import is_weak
from .orchestrator import MAX_TURNS, OrchestratorError, _Trace
from .orchestrator import TOOLS as ORCH_TOOLS
from .redact import Redactor

log = logging.getLogger(__name__)

LEFT_ON = frozenset({"light", "media_player", "fan", "switch"})
ON_STATES = frozenset({"on", "playing", "paused"})
MAX_TASKS_PER_RUN = 3
MAX_CALLS_PER_RUN = 6
MAX_PROPOSALS_PER_RUN = 1
TEXT_MAX = 500
# Network-level switches no house manager should flip: opening the Zigbee / Z-Wave / Thread network
# to new devices. Usually `entity_category: config` too, but not always (Zigbee2MQTT's bridge).
ENTITY_ID = re.compile(r"\b[a-z][a-z0-9_]*\.[a-z0-9_]+\b")
NEVER_OWNED = re.compile(r"_(permit_join|permit_joining|inclusion_mode|commissioning)$")
OWNED_NOTE = (
    "Owned: switchable devices that no room agent looks after (no area, or an area without a room "
    "agent), so the house manager is the one that may act on them. Device settings and diagnostics "
    "(entity_category config / diagnostic), hidden or disabled entities and network pairing "
    "switches are never owned. The manager is {enabled}. Off: it is never woken by itself and switches "
    "nothing; a manual wake still runs it once to look, give room agents tasks, propose drafts and "
    "report to you. On: it is also woken by the sweep and may switch these, through the safety "
    "policy, in {mode} mode (shadow: recorded, not done). Give a room "
    "an agent, or set its area in Home Assistant, to hand an entity to that room."
)

SYSTEM = """You are the house manager of Agent House, the orchestrator of a smart home. Room agents \
(small local models) look after single rooms; you look after the whole house between the owner's \
daily reviews. You are woken only when something needs attention; the reasons are listed.

What you can do:
- call_service on entities listed under "Yours" (no room agent owns them). Everything goes through \
the safety policy; the result tells you if it ran, ran in shadow, waits for the owner or was denied.
- assign_task to the room agent that owns an entity in its room ("Rooms"): one clear sentence.
- propose_automation when something keeps happening: a new automation, or a change to one of ours \
(give its ha_id). The owner reviews every proposal; nothing is deployed by you.
- report to the owner when they must know or decide something.
- find_entities to look entities up.
The context lists what the deterministic check found. Each finding must be handled: act on what \
is yours, assign_task to the room agent that owns the room, or report it to the owner when nobody \
can act on it (for example lights on in a room without an agent while nobody is home). Never leave \
a finding unmentioned in your final sentence.
Rules: act only on what the data shows; never invent entity ids or states. Prefer the smallest \
action. Do not repeat what a room agent is already doing. When done, answer with one short sentence \
and no tool call."""

TOOLS = [
    ORCH_TOOLS[0],  # find_entities
    {
        "name": "call_service",
        "description": "Call a Home Assistant service on one entity you own (see 'Yours').",
        "properties": {
            "domain": {"type": "string"},
            "service": {"type": "string"},
            "entity_id": {"type": "string"},
            "data": {"type": "object", "description": "service data, or {}"},
        },
        "required": ["domain", "service", "entity_id", "data"],
    },
    {
        "name": "assign_task",
        "description": "Give a room agent a task in its own room, in one sentence.",
        "properties": {"agent_id": {"type": "string"}, "task": {"type": "string"}},
        "required": ["agent_id", "task"],
    },
    {
        "name": "propose_automation",
        "description": "Ask for an automation draft for the owner's review. ha_id: one of ours to "
        "change (see 'Our automations'), or empty for a new one.",
        "properties": {"request": {"type": "string"}, "ha_id": {"type": "string"}},
        "required": ["request", "ha_id"],
    },
    {
        "name": "report",
        "description": "Tell the owner something in their inbox.",
        "properties": {
            "severity": {"type": "string", "enum": ["info", "warning", "alarm"]},
            "summary": {"type": "string"},
        },
        "required": ["severity", "summary"],
    },
]


class Manager:
    """Wiring (app factory): `orch`, `council`, `actuator`, `metrics` and the runtime settings."""

    def __init__(self, orch, council, actuator, metrics, settings, clock=time.time) -> None:
        self.orch, self.council, self.actuator, self.metrics = orch, council, actuator, metrics
        self.settings, self.clock = settings, clock
        self.store, self.feed, self.ha, self.safety = orch.store, orch.feed, orch.ha, orch.safety
        self._wakes: deque[float] = deque()
        self._pending: list[str] = []
        self._task: asyncio.Task | None = None
        self._last_sweep = clock()
        self._last_run = 0.0
        self._last_run_id: str | None = None
        self.weak: list[dict] = []  # automations flagged by the last metrics read

    # --- settings ---------------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "manager_enabled", False))

    def _cap(self) -> int:
        return int(getattr(self.settings, "manager_max_wakes_per_hour", 4))

    # --- scope: what the actuator lets the orchestrator touch ---------------------------------
    def owned(self) -> list[str]:
        """Entities the manager may actuate: owned by the orchestrator, of a switchable domain."""
        registry = self.ha.registry
        return [
            e for e in self.council.ownership.owned_by(ORCHESTRATOR)
            if e.split(".", 1)[0] in ACT_DOMAINS and not registry.is_hidden(e)
            and not registry.entity_meta.get(e, {}).get("entity_category")
            and not NEVER_OWNED.search(e) and not is_noise(self.ha, e)
        ]  # fmt: skip

    def scope(self) -> set[str]:
        return {MODE_ENTITY, *(self.owned() if self.enabled else [])}

    # --- deterministic prefilter -------------------------------------------------------------
    def anomalies(self) -> list[str]:
        since = self._last_run or (self.clock() - 3600)
        out: list[str] = []
        mode = self.council.mode
        people = [s.get("state") for e, s in self.ha.states.items() if e.startswith("person.")]
        nobody = bool(people) and not any(p == "home" for p in people)
        if nobody or mode in ("away", "night"):
            left = sorted(
                e for e, s in self.ha.states.items()
                if e.split(".", 1)[0] in LEFT_ON and s.get("state") in ON_STATES
                and (mode != "night" or e.startswith("media_player."))
                and not self.ha.registry.is_hidden(e)
            )  # fmt: skip
            if left:
                why = "nobody is home" if nobody else f"house mode is {mode}"
                out.append(f"{why} and still on: {', '.join(left[:12])}")
        rows = self.store.query(
            "SELECT agent_id, status, COUNT(*) AS n FROM runs WHERE started>=? AND agent_id<>? "
            "AND status IN ('error', 'escalated', 'timeout') GROUP BY agent_id, status",
            (since, ORCHESTRATOR),
        )
        out += [f"room agent {r['agent_id']}: {r['n']} run(s) {r['status']}" for r in rows]
        out += [
            f"our automation {w['ha_id']} ({w.get('title')}): precision {w['precision']['value']}, "
            f"coverage {w['coverage']['value']} "
            f"(kept {w['kept']}, undone {w['undone']}, missed {w['missed']})"
            for w in self.weak
        ]
        return out

    async def refresh_metrics(self) -> None:
        """Which of ours look weak (metrics.is_weak). Failures keep the old list."""
        if self.metrics is None:
            return
        try:
            result = await self.metrics.compute()
        except Exception as err:  # noqa: BLE001 - the logbook may be unreachable; try next sweep
            log.info("manager: metrics unavailable: %s", err)
            return
        self.weak = [a for a in result["automations"] if is_weak(a)]

    # --- wakes --------------------------------------------------------------------------------
    def on_escalation(self, agent_id: str, summary: str) -> None:
        """An escalation stayed on the board for the orchestrator."""
        if self.enabled:
            self.request(f"escalation from {agent_id}: {summary}"[:TEXT_MAX])

    async def tick(self) -> None:
        """From the council loop: the sweep, when due and when the prefilter finds something."""
        every = float(getattr(self.settings, "manager_sweep_min", 60)) * 60
        if not self.enabled or every <= 0 or self.clock() - self._last_sweep < every:
            return
        self._last_sweep = self.clock()
        self.council.wake_fired("manager_sweep")
        await self.refresh_metrics()
        found = self.anomalies()
        if found:
            self.request("sweep: " + "; ".join(found))
        else:
            self.feed.add("gate_drop", "manager: sweep found nothing, no model call", level="debug",
                          agent_id=ORCHESTRATOR)  # fmt: skip

    def request(self, reason: str, *, force: bool = False) -> dict:
        """Queue a wake; coalesces while a run is going. → {status, ...}."""
        if self.safety.kill_engaged:
            return {"status": "refused", "detail": "the kill switch is engaged"}
        if not self.enabled and not force:
            return {"status": "refused", "detail": "manager_enabled is off"}
        now = self.clock()
        while self._wakes and now - self._wakes[0] > 3600:
            self._wakes.popleft()
        if len(self._wakes) >= self._cap():
            self.feed.add("gate_drop", f"manager: not woken, {self._cap()} per hour", level="warning",
                          agent_id=ORCHESTRATOR)  # fmt: skip
            return {"status": "refused", "detail": f"at most {self._cap()} wakes per hour"}
        self._pending.append(reason[:TEXT_MAX])
        if self._task is not None and not self._task.done():
            return {"status": "queued"}
        self._wakes.append(now)
        self._task = asyncio.get_running_loop().create_task(self._drain(), name="manager")
        return {"status": "started"}

    async def _drain(self) -> None:
        while self._pending:
            reasons, self._pending = self._pending, []
            try:
                await self.run(reasons)
            except Exception:  # noqa: BLE001 - one failed run must not stop the next
                log.exception("manager run failed")

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    # --- one run --------------------------------------------------------------------------------
    def context(self, reasons: list[str], found: list[str] | None = None) -> str:
        lines = [f"Woken because: {'; '.join(reasons)}"]
        if found:  # a manual wake or an escalation still sees what the sweep would have
            lines.append("Found by the deterministic check:")
            lines += [f"- {f}" for f in found]
        lines.append(self.orch.house_summary())
        people = {e: s.get("state") for e, s in self.ha.states.items() if e.startswith("person.")}
        if people:
            lines.append("People: " + ", ".join(f"{e} = {v}" for e, v in sorted(people.items())))
        agents = [a for a in self.council.agents.list() if a.enabled]
        if agents:
            lines.append("Rooms (agent: room): " + ", ".join(f"{a.id}: {a.room}" for a in agents))
        mine = self.owned() if self.enabled else []
        lines.append("Yours (no room agent owns them):")
        lines += [f"- {e} = {(self.ha.states.get(e) or {}).get('state')}" for e in mine[:40]] or [
            "(none: the manager is off, you switch nothing; assign, propose or report)"
            if not self.enabled
            else "(none)"
        ]
        board = [r for r in self.council.board.since(self.clock() - 6 * 3600) if r["kind"] != "fact"]
        if board:
            lines.append("Board (last 6 h):")
            lines += [f"- {r['agent_id']} [{r['kind']}]: {r['text']}" for r in board[-12:]]
        deployed = self.orch.lifecycle.deployed()
        if deployed:
            lines.append("Our automations (ha_id: title):")
            lines += [f"- {d['ha_id']}: {d.get('title')}" for d in deployed[:30]]
        if self.weak:
            lines.append("Weak automations (from the accuracy metrics):")
            lines += [f"- {w['ha_id']}: {', '.join(w['examples'][:2])}" for w in self.weak[:5]]
        return "\n".join(lines)

    async def run(self, reasons: list[str]) -> str:
        """One bounded conversation. → run id."""
        found = [f for f in self.anomalies() if not any(f in r for r in reasons)]
        self._last_run = self.clock()
        user = self.context(reasons, found)
        trace = _Trace(self.orch, "manager: " + "; ".join(reasons), "manage")
        self._last_run_id = trace.run_id
        shadow = getattr(self.settings, "orchestrator_mode", "shadow") != "live" or self.safety.mode != "live"
        self.store.execute("UPDATE runs SET shadow=? WHERE id=?", (int(shadow), trace.run_id))
        trace.step("context", {"system": SYSTEM, "user": user})
        redactor = Redactor.for_home(self.ha)
        transcript: list[dict] = [{"role": "user", "text": user}]
        used = {"call_service": 0, "assign_task": 0, "propose_automation": 0}
        final = ""
        try:
            for _ in range(MAX_TURNS):
                turn = await self.orch.turn(
                    SYSTEM, transcript, TOOLS, Priority.BACKGROUND, redactor, "manage"
                )
                trace.llm(turn)
                if not turn.calls:
                    final = str(redactor.restore(turn.text)).strip()
                    break
                results = [await self._call(call, used, trace) for call in turn.calls]
                transcript += [
                    {"role": "assistant", "text": turn.text, "calls": turn.calls, "raw": turn.raw,
                     "raw_by": turn.raw_by},
                    {"role": "tool", "results": results},
                ]  # fmt: skip
        except OrchestratorError as err:
            trace.finish("error", error=str(err))
            self.feed.add(
                "council", f"manager: not run ({err})"[:300], level="warning", agent_id=ORCHESTRATOR
            )
            return trace.run_id
        if not final:
            trace.finish("escalated", error="no answer within the turn limit")
            return trace.run_id
        trace.step("final", {"text": final})
        trace.finish("ok", summary=final[:300])
        self.feed.add("council", f"manager: {final}"[:300], agent_id=ORCHESTRATOR, run_id=trace.run_id)
        return trace.run_id

    async def _call(self, call: dict, used: dict, trace) -> dict:
        def answer(content, error: bool = False) -> dict:
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            trace.step(
                "tool", {"name": call.get("name"), "arguments": call.get("input"), "result": text[:2000]}
            )
            return {"id": call["id"], "content": text, "is_error": error}

        if call.get("error"):
            return answer(f"unusable call: {call['error']}", True)
        name, args = call["name"], call.get("input") or {}
        if name == "find_entities":
            return answer(
                self.orch.find_entities(str(args.get("query") or ""), str(args.get("area") or "") or None)
            )
        if name in used:
            limit = {"call_service": MAX_CALLS_PER_RUN, "assign_task": MAX_TASKS_PER_RUN,
                     "propose_automation": MAX_PROPOSALS_PER_RUN}[name]  # fmt: skip
            if used[name] >= limit:
                return answer(f"limit reached: at most {limit} {name} per run", True)
            used[name] += 1
        if name == "call_service":
            entity = str(args.get("entity_id") or "")
            data = args.get("data") if isinstance(args.get("data"), dict) else {}
            action = await self.actuator.request(
                ORCHESTRATOR, str(args.get("domain") or ""), str(args.get("service") or ""), entity, data,
                run_id=trace.run_id,
            )  # fmt: skip
            return answer(
                {"verdict": action["verdict"], "reason": action["reason"]}, action["verdict"] == "denied"
            )
        if name == "assign_task":
            agent_id, text = str(args.get("agent_id") or ""), str(args.get("task") or "").strip()[:300]
            if not text:
                return answer("empty task", True)
            run_id = self.council.assign_task(agent_id, text, asked_by=ORCHESTRATOR)
            return answer({"given": bool(run_id), "run_id": run_id}, not run_id)
        if name == "propose_automation":
            return answer(await self._propose(args, trace))
        if name == "report":
            severity = (
                args.get("severity") if args.get("severity") in ("info", "warning", "alarm") else "info"
            )
            summary = str(args.get("summary") or "").strip()[:TEXT_MAX]
            if not summary:
                return answer("empty report", True)
            domains = {e.split(".", 1)[0] for e in self.ha.states}
            named = set(ENTITY_ID.findall(summary))
            invented = sorted(m for m in named if m.split(".", 1)[0] in domains and m not in self.ha.states)
            if invented:  # QA-005: the owner must not be told about entities that do not exist
                return answer(
                    f"not sent: unknown entity ids {', '.join(invented)}; use ids from the context "
                    "or find_entities, or describe them in words",
                    True,
                )
            self.orch.agents_rt.inbox_add(ORCHESTRATOR, trace.run_id, severity, summary)
            return answer("in the owner's inbox")
        return answer(f"unknown tool {name}", True)

    async def _propose(self, args: dict, trace) -> dict:
        request = str(args.get("request") or "").strip()[:2000]
        ha_id = str(args.get("ha_id") or "").strip() or None
        if not request:
            return {"error": "empty request"}
        if ha_id and not self.orch.lifecycle.deployed(ha_id):
            return {"error": f"{ha_id} is not one of our deployed automations"}

        async def job() -> None:
            try:
                await self.orch.draft(
                    request, origin_agent=ORCHESTRATOR, run_id=trace.run_id, ha_id=ha_id,
                    priority=Priority.BACKGROUND, task="undo" if ha_id else "draft",
                )  # fmt: skip
            except Exception as err:  # noqa: BLE001 - OrchestratorError, LifecycleError
                self.feed.add("proposal", f"manager: no draft ({err})"[:300], level="warning",
                              agent_id=ORCHESTRATOR)  # fmt: skip

        self.orch.spawn(job(), "manager-draft")
        return {"status": "drafting", "ha_id": ha_id, "note": "the owner reviews it on the Proposals page"}

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "mode": getattr(self.settings, "orchestrator_mode", "shadow"),
            "sweep_min": getattr(self.settings, "manager_sweep_min", 60),
            "max_wakes_per_hour": self._cap(),
            "wakes_last_hour": sum(1 for t in self._wakes if self.clock() - t <= 3600),
            "running": self._task is not None and not self._task.done(),
            "last_run": self._last_run or None,
            "last_run_id": self._last_run_id,
            "anomalies": self.anomalies(),
            "owned": self.owned(),
            "owned_note": OWNED_NOTE.format(
                enabled="enabled" if self.enabled else "disabled",
                mode=getattr(self.settings, "orchestrator_mode", "shadow"),
            ),
            "weak": [{"ha_id": w["ha_id"], "title": w.get("title")} for w in self.weak],
            "checked": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
