"""The tool layer: everything a room agent can do. Each tool checks the agent's scope itself;
`call_service` hands over to the actuator (policy → action log → HA) and to nothing else.

Tool results are small JSON objects with `ok`; errors are sentences the model can act on.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from .benchkit import TOOLS as BASE_TOOLS
from .benchkit import _tool, normalize_call

LIST_LIMIT = 25  # rows per list_entities answer; more are announced, not sent

TOOLS: list[dict] = [
    *BASE_TOOLS,  # get_state, call_service, report: the shapes the benchmark measured
    _tool(
        "list_entities",
        "Find entities of your room that your prompt does not list, with their state and tier.",
        {
            "domain": {"type": "string", "description": "optional filter, e.g. light"},
            "search": {"type": "string", "description": "optional word in the id or name, e.g. heater"},
        },
        [],
    ),
    _tool(
        "get_history",
        "Recent state changes of one entity in your room.",
        {"entity_id": {"type": "string"}, "hours": {"type": "number", "description": "1 to 48, default 6"}},
        ["entity_id"],
    ),
    _tool(
        "ask_agent",
        "Ask about or request something another agent owns. It goes through the orchestrator, which "
        "gives the owner a task; the answer appears on the shared board later, not here.",
        {
            "agent_id": {"type": "string", "description": "the agent, or orchestrator if unsure"},
            "question": {"type": "string", "description": "one sentence; name the entity id"},
        },
        ["agent_id", "question"],
    ),
    _tool(
        "post_fact",
        "Tell your neighbours a short fact about your room on the shared board.",
        {
            "text": {"type": "string", "description": "e.g. someone went to bed at 23:10"},
            "minutes": {"type": "number", "description": "how long it stays true, 5 to 720, default 60"},
        },
        ["text"],
    ),
    _tool(
        "schedule_wake",
        "Ask to be woken again once, later, to check on something.",
        {"in_minutes": {"type": "number", "description": "1 to 1440"}, "reason": {"type": "string"}},
        ["in_minutes", "reason"],
    ),
    _tool(
        "remember",
        "Save a short note for your future wakes. An empty value deletes the note.",
        {"key": {"type": "string"}, "value": {"type": "string"}},
        ["key", "value"],
    ),
    _tool("recall", "Read your saved notes.", {"key": {"type": "string", "description": "optional"}}, []),
    _tool(
        "propose_automation",
        "Suggest a permanent Home Assistant automation for your room. It is drafted and the owner "
        "reviews it; nothing changes until then.",
        {
            "description": {
                "type": "string",
                "description": "what should happen, when, e.g. lamp on at motion after dark",
            }
        },
        ["description"],
    ),
]
TOOL_NAMES = [tool["function"]["name"] for tool in TOOLS]
READ_TOOLS = frozenset({"get_state", "list_entities", "get_history", "recall"})
USAGE_TOOLS = frozenset({"get_state", "get_history", "call_service"})  # what ranks an agent's prompt

ATTRIBUTES = (
    "brightness",
    "temperature",
    "current_temperature",
    "humidity",
    "current_position",
    "percentage",
    "volume_level",
    "hvac_action",
    "device_class",
)
SEVERITY = {"info": "info", "warning": "warning", "escalate": "alarm"}
CALL_KEYS = {"domain", "service", "entity_id", "data"}


def compact_attributes(state: dict) -> dict:
    attrs = state.get("attributes") or {}
    return {key: attrs[key] for key in ATTRIBUTES if attrs.get(key) is not None}


def state_text(state: dict) -> str:
    """`on`, `20.5 °C`, `heat {"temperature": 21}`: one entity's state for the prompt."""
    attrs = state.get("attributes") or {}
    text = str(state.get("state"))[:60]
    if attrs.get("unit_of_measurement"):
        text += f" {attrs['unit_of_measurement']}"
    extra = {k: v for k, v in compact_attributes(state).items() if k != "device_class"}
    if extra:
        text += " " + ", ".join(f"{key}={value}" for key, value in extra.items())
    return text


class ToolBox:
    """The tools of one run. `rt` is the agent runtime (stores, HA, actuator, gate)."""

    def __init__(self, rt, agent, run: dict, scope: list[str]) -> None:
        self.rt, self.agent, self.run = rt, agent, run
        self.scope = list(scope)
        self.dry = bool(run.get("replay_of"))  # replays leave no trace outside their own run
        self.actions: list[dict] = []  # policy decisions of the call being executed
        self.asked = False
        self.scheduled = False
        self.proposed = False
        self.posted = 0

    async def execute(self, name: str, args: dict) -> dict:
        handler = getattr(self, f"_{name}", None) if name in TOOL_NAMES else None
        if handler is None:
            return {"ok": False, "error": f"unknown tool {name!r}"}
        self.actions = []
        args = args if isinstance(args, dict) else {}
        try:
            out = await handler(args)
        except Exception as err:  # noqa: BLE001 - a tool bug is a tool error, not a dead run
            self.rt.log.exception("tool %s failed", name)
            return {"ok": False, "error": f"{name} failed: {type(err).__name__}"}
        entity_id = args.get("entity_id")
        if name in USAGE_TOOLS and not self.dry and isinstance(entity_id, str) and entity_id in self.scope:
            self.rt.record_use(self.agent.id, entity_id)
        return out

    def _entity(self, args: dict) -> tuple[str | None, dict | None]:
        entity_id = args.get("entity_id")
        if not isinstance(entity_id, str) or entity_id not in self.scope:
            return None, {"ok": False, "error": f"unknown entity {entity_id!r}; use an id from your room"}
        if entity_id not in self.rt.ha.states:
            return None, {"ok": False, "error": f"{entity_id} has no state right now"}
        return entity_id, None

    async def _get_state(self, args: dict) -> dict:
        entity_id, error = self._entity(args)
        if error:
            return error
        state = self.rt.ha.states[entity_id]
        attrs = state.get("attributes") or {}
        out = {"ok": True, "state": state.get("state"), "attributes": compact_attributes(state)}
        if attrs.get("unit_of_measurement"):
            out["unit"] = attrs["unit_of_measurement"]
        return out

    async def _list_entities(self, args: dict) -> dict:
        domain = args.get("domain") if isinstance(args.get("domain"), str) else None
        search = args.get("search").strip().lower() if isinstance(args.get("search"), str) else ""
        matches = [
            entity_id
            for entity_id in self.scope
            if entity_id in self.rt.ha.states
            and (not domain or entity_id.split(".", 1)[0] == domain)
            and (not search or search in f"{entity_id} {self.rt._name(entity_id)}".lower())
        ]
        rows = [
            {
                "entity_id": entity_id,
                "name": self.rt._name(entity_id),
                "state": state_text(self.rt.ha.states[entity_id]),
                "tier": self.rt._label(self.agent, entity_id),
            }
            for entity_id in matches[:LIST_LIMIT]
        ]
        out = {"ok": True, "entities": rows}
        if len(matches) > LIST_LIMIT:
            out["more"] = f"{len(matches) - LIST_LIMIT} more: narrow down with domain or search"
        return out

    async def _get_history(self, args: dict) -> dict:
        entity_id, error = self._entity(args)
        if error:
            return error
        hours = args.get("hours", 6)
        hours = min(48.0, max(1.0, float(hours))) if isinstance(hours, int | float) else 6.0
        end = datetime.now(UTC)
        try:
            rows = await self.rt.ha.history(
                entity_id, (end - timedelta(hours=hours)).isoformat(), end.isoformat()
            )
        except Exception as err:  # noqa: BLE001
            return {"ok": False, "error": f"history is unavailable: {err}"[:200]}
        changes, last = [], None
        for row in rows:
            if not isinstance(row, dict) or row.get("state") == last:
                continue
            last = row.get("state")
            changes.append({"at": str(row.get("last_changed") or "")[:16], "state": last})
        return {"ok": True, "hours": hours, "changes": changes[-20:]}

    async def _call_service(self, args: dict) -> dict:
        extra = sorted(set(args) - CALL_KEYS)
        if extra:
            return {"ok": False, "error": f'unexpected argument {extra[0]!r}; service data goes into "data"'}
        data = args.get("data", {})
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return {"ok": False, "error": '"data" must be an object'}
        results = []
        for call in normalize_call("call_service", {**args, "data": data}):
            action = await self.rt.actuator.request(
                self.agent.id,
                call.get("domain"),
                call.get("service"),
                call.get("entity_id"),
                data,
                run_id=self.run["id"],
                force_shadow=self.dry,
            )
            self.actions.append(action)
            verdict = action["verdict"]
            if verdict == "executed":
                results.append({"ok": True, "status": "executed"})
            elif verdict == "shadow":
                note = "shadow mode: logged, not sent. Treat it as done; do not retry."
                results.append({"ok": True, "status": "recorded", "note": note})
            elif verdict == "pending":
                results.append({"ok": True, "status": "pending_approval", "note": "the owner has been asked"})
            elif verdict == "denied":
                results.append({"ok": False, "error": f"denied by policy: {action['reason']}"})
            else:
                results.append({"ok": False, "error": action["reason"]})
        if len(results) == 1:
            return results[0]
        return {"ok": all(r["ok"] for r in results), "results": results}

    async def _report(self, args: dict) -> dict:
        severity, summary = args.get("severity"), args.get("summary")
        if severity not in SEVERITY:
            return {"ok": False, "error": "severity must be info, warning or escalate"}
        if not isinstance(summary, str) or not summary.strip():
            return {"ok": False, "error": "summary must be a short sentence"}
        self.run["reported"] = summary.strip()[:300]
        if severity == "escalate":
            self.run["escalated"] = True
        if not self.dry:
            self.rt.inbox_add(self.agent.id, self.run["id"], SEVERITY[severity], summary.strip()[:500])
            council = getattr(self.rt, "council", None)
            if severity == "escalate" and council is not None:
                routed = council.escalate(
                    self.agent.id, summary.strip(), self.run["id"], source=self.run["wake_source"]
                )
                return {"ok": True, **routed}
        return {"ok": True}

    async def _ask_agent(self, args: dict) -> dict:
        """Through the orchestrator (council), never straight to a neighbour; depth 1."""
        target, question = args.get("agent_id"), args.get("question")
        if not isinstance(question, str) or not question.strip():
            return {"ok": False, "error": "question must be a sentence"}
        if self.run["wake_source"] in ("poke", "schedule", "task") or self.asked:
            return {"ok": False, "error": "one question per wake, and none on a task, a wake by another "
                    "agent or a schedule. Finish with one short sentence."}  # fmt: skip
        council = getattr(self.rt, "council", None)
        if council is None:
            return {"ok": False, "error": "nobody to ask; report it instead"}
        self.asked = True
        if self.dry:
            return {"ok": True, "status": "forwarded"}
        return council.ask(self.agent.id, target, question.strip()[:300], self.run["id"])

    async def _post_fact(self, args: dict) -> dict:
        text, minutes = args.get("text"), args.get("minutes", 60)
        if not isinstance(text, str) or len(text.strip()) < 3:
            return {"ok": False, "error": "text must be a short sentence"}
        if isinstance(minutes, bool) or not isinstance(minutes, int | float) or not 5 <= minutes <= 720:
            return {"ok": False, "error": "minutes must be a number from 5 to 720"}
        if self.posted >= 2:
            return {"ok": False, "error": "two facts per wake. Finish with one short sentence."}
        council = getattr(self.rt, "council", None)
        if council is None:
            return {"ok": False, "error": "there is no shared board"}
        self.posted += 1
        if not self.dry:
            council.board.post(self.agent.id, text, ttl_s=float(minutes) * 60, run_id=self.run["id"])
        return {"ok": True, "status": "posted"}

    async def _schedule_wake(self, args: dict) -> dict:
        minutes, reason = args.get("in_minutes"), args.get("reason")
        if isinstance(minutes, bool) or not isinstance(minutes, int | float) or not 1 <= minutes <= 1440:
            return {"ok": False, "error": "in_minutes must be a number from 1 to 1440"}
        if not isinstance(reason, str) or not reason.strip():
            return {"ok": False, "error": "reason must be a sentence"}
        if self.run["wake_source"] in ("poke", "schedule") or self.scheduled:
            return {"ok": False, "error": "one schedule_wake per wake, and none on a wake by another agent "
                    "or a schedule. Finish with one short sentence."}  # fmt: skip
        self.scheduled = True
        if not self.dry and not self.rt.gate.schedule(self.agent.id, minutes * 60, reason.strip()):
            return {"ok": False, "error": "you already have the maximum number of scheduled wakes"}
        return {"ok": True, "status": "scheduled"}

    async def _propose_automation(self, args: dict) -> dict:
        description = args.get("description")
        if not isinstance(description, str) or len(description.strip()) < 10:
            return {"ok": False, "error": "description must say what should happen and when"}
        if self.proposed:
            return {"ok": False, "error": "one proposal per wake. Finish with one short sentence."}
        self.proposed = True
        orchestrator = getattr(self.rt, "orchestrator", None)
        if self.dry:
            return {"ok": True, "status": "queued"}
        if orchestrator is None or not orchestrator.request_draft(
            self.agent.id, self.run["id"], description.strip()[:1000], self.agent.room
        ):
            return {
                "ok": False,
                "error": "the orchestrator is busy or unavailable; mention it in your answer",
            }
        return {"ok": True, "status": "queued", "note": "the owner reviews it on the Proposals page"}

    async def _remember(self, args: dict) -> dict:
        key, value = args.get("key"), args.get("value")
        if not isinstance(key, str) or not 0 < len(key.strip()) <= 40:
            return {"ok": False, "error": "key must be 1 to 40 characters"}
        if not isinstance(value, str) or len(value) > 300:
            return {"ok": False, "error": "value must be text of at most 300 characters"}
        key, value = key.strip(), value.strip()
        if self.dry:
            return {"ok": True}
        notes = self.rt.memory(self.agent.id)
        notes.pop(key, None)
        if not value:
            self.rt.store.execute("DELETE FROM memory WHERE agent_id=? AND key=?", (self.agent.id, key))
            return {"ok": True, "status": "deleted"}
        used = sum(len(k) + len(v) for k, v in notes.items())
        if used + len(key) + len(value) > self.agent.memory_cap_chars:
            return {"ok": False, "error": "memory is full; delete a note first (empty value)"}
        self.rt.store.execute(
            "INSERT INTO memory (agent_id, key, value, ts) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(agent_id, key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            (self.agent.id, key, value, time.time()),
        )
        return {"ok": True, "status": "saved"}

    async def _recall(self, args: dict) -> dict:
        notes = self.rt.memory(self.agent.id)
        key = args.get("key")
        if isinstance(key, str) and key.strip():
            notes = {k: v for k, v in notes.items() if k == key.strip()}
        return {"ok": True, "notes": notes}
