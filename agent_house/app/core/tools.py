"""The tool layer: everything a room agent can do. Each tool checks the agent's scope itself;
`call_service` hands over to the actuator (policy → action log → HA) and to nothing else.

Tool results are small JSON objects with `ok`; errors are sentences the model can act on.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from .benchkit import TOOLS as BASE_TOOLS
from .benchkit import _tool, normalize_call

TOOLS: list[dict] = [
    *BASE_TOOLS,  # get_state, call_service, report: the shapes the benchmark measured
    _tool(
        "list_entities",
        "List the entities of your room with their current state and tier.",
        {"domain": {"type": "string", "description": "optional filter, e.g. light"}},
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
        "Send a question or a request to another room's agent. It is woken later; you get no answer here.",
        {"agent_id": {"type": "string"}, "question": {"type": "string"}},
        ["agent_id", "question"],
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
]
TOOL_NAMES = [tool["function"]["name"] for tool in TOOLS]

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

    async def execute(self, name: str, args: dict) -> dict:
        handler = getattr(self, f"_{name}", None) if name in TOOL_NAMES else None
        if handler is None:
            return {"ok": False, "error": f"unknown tool {name!r}"}
        self.actions = []
        try:
            return await handler(args if isinstance(args, dict) else {})
        except Exception as err:  # noqa: BLE001 - a tool bug is a tool error, not a dead run
            self.rt.log.exception("tool %s failed", name)
            return {"ok": False, "error": f"{name} failed: {type(err).__name__}"}

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
        domain = args.get("domain")
        rows = [
            {
                "entity_id": entity_id,
                "state": state_text(self.rt.ha.states[entity_id]),
                "tier": self.rt.tier(self.agent, entity_id),
            }
            for entity_id in self.scope
            if entity_id in self.rt.ha.states and (not domain or entity_id.split(".", 1)[0] == domain)
        ]
        return {"ok": True, "entities": rows}

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
        return {"ok": True}

    async def _ask_agent(self, args: dict) -> dict:
        target, question = args.get("agent_id"), args.get("question")
        other = self.rt.agents.get(target) if isinstance(target, str) else None
        if other is None or not other.enabled or other.id == self.agent.id:
            known = [a.id for a in self.rt.agents.list() if a.enabled and a.id != self.agent.id]
            return {"ok": False, "error": f"unknown agent {target!r}; agents: {', '.join(known) or 'none'}"}
        if not isinstance(question, str) or not question.strip():
            return {"ok": False, "error": "question must be a sentence"}
        if self.run["wake_source"] == "poke" or self.asked:
            return {"ok": False, "error": "one question per wake, and none while answering another agent"}
        self.asked = True
        if not self.dry:
            reason = f'Agent "{self.agent.id}" asks: {question.strip()[:300]}'
            try:
                self.rt.wake(other.id, source="poke", reason=reason)
            except Exception as err:  # noqa: BLE001
                return {"ok": False, "error": f"{other.id} cannot be woken: {err}"[:200]}
        return {
            "ok": True,
            "status": "queued",
            "note": "its answer goes to the owner as a report, not to you",
        }

    async def _schedule_wake(self, args: dict) -> dict:
        minutes, reason = args.get("in_minutes"), args.get("reason")
        if isinstance(minutes, bool) or not isinstance(minutes, int | float) or not 1 <= minutes <= 1440:
            return {"ok": False, "error": "in_minutes must be a number from 1 to 1440"}
        if not isinstance(reason, str) or not reason.strip():
            return {"ok": False, "error": "reason must be a sentence"}
        if not self.dry and not self.rt.gate.schedule(self.agent.id, minutes * 60, reason.strip()):
            return {"ok": False, "error": "you already have the maximum number of scheduled wakes"}
        return {"ok": True, "status": "scheduled"}

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
