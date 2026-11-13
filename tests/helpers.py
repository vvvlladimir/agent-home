"""In-process stand-ins for unit tests of the agent stack: a stub Home Assistant, a scripted model
and the wiring the application factory does."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.core.actuator import Actuator
from app.core.agents import AgentStore, parse_agent
from app.core.council import Council
from app.core.gate import EventGate
from app.core.llm import LLMResult
from app.core.runtime import AgentRuntime
from app.core.safety import Safety
from app.core.toolcalls import extract_tool_calls, visible_text
from app.core.tools import TOOL_NAMES
from app.ha.client import HAError
from app.ha.registry import Registry
from app.store.db import Store
from app.store.feed import Feed
from sim.fake_llm import LLMSim, _message

ROOM = [
    {"entity_id": "light.room_ceiling", "state": "off"},
    {"entity_id": "light.room_lamp", "state": "off"},
    {"entity_id": "scene.room_movie", "state": "scening"},
    {"entity_id": "climate.room", "state": "heat", "attributes": {"temperature": 21}},
    {"entity_id": "switch.room_heater", "state": "off"},
    {"entity_id": "lock.room_door", "state": "locked"},
    {"entity_id": "binary_sensor.room_motion", "state": "off"},
    {"entity_id": "sensor.room_illuminance", "state": "4", "attributes": {"unit_of_measurement": "lx"}},
]
AGENT = {"id": "room", "name": "Room", "room": "room"}


class StubHA:
    def __init__(self, entities: list[dict] | None = None, area: str = "room", name: str = "Room") -> None:
        entities = ROOM if entities is None else entities
        self.states = {
            e["entity_id"]: {
                "entity_id": e["entity_id"],
                "state": e["state"],
                "attributes": {
                    **(e.get("attributes") or {}),
                    "friendly_name": e.get("name") or e["entity_id"],
                },
            }
            for e in entities
        }
        self.registry = Registry.build(
            [{"area_id": area, "name": name}], [], [{"entity_id": e, "area_id": area} for e in self.states]
        )
        self.calls: list[dict] = []
        self.fail: str | None = None
        self.before_call = None  # hook: runs inside call_service before anything is recorded

    def add(self, entity_id: str, state: str, area: str | None = None) -> None:
        self.states[entity_id] = {"entity_id": entity_id, "state": state, "attributes": {}}
        self.registry.entity_area[entity_id] = area

    async def call_service(self, domain, service, entity_id, data=None) -> None:
        if self.before_call:
            self.before_call()
        if self.fail:
            raise HAError(self.fail)
        self.calls.append({"domain": domain, "service": service, "entity_id": entity_id, "data": data or {}})
        if service in ("turn_on", "turn_off") and entity_id in self.states:
            self.states[entity_id] = {**self.states[entity_id], "state": service[5:]}

    async def history(self, entity_id, start, end) -> list[dict]:
        return [
            {"state": "off", "last_changed": "2026-11-13T20:00:00+00:00"},
            {"state": "on", "last_changed": "2026-11-13T21:00:00+00:00"},
        ]


def call(name: str, **arguments) -> dict:
    """One scripted model turn with one tool call."""
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


def turn_on(entity_id: str, **extra) -> dict:
    return call(
        "call_service", domain=entity_id.split(".")[0], service="turn_on", entity_id=entity_id, **extra
    )


class ScriptedLLM:
    """Answers `chat()` from a list of fake-llama-server steps (sim/fake_llm.py), then says "Done."."""

    active_profile = "scripted"

    def __init__(self, steps: list[dict] | None = None) -> None:
        self.steps = list(steps or [])
        self.requests: list[dict] = []
        self._sim = LLMSim()

    async def chat(self, messages, tools=None, *, max_tokens=None, **options) -> LLMResult:
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools, **options})
        step = self.steps.pop(0) if self.steps else {"text": "Done."}
        if step.get("raise"):
            raise step["raise"]
        if step.get("sleep"):
            await asyncio.sleep(step["sleep"])
        message, finish = _message(step, self._sim)
        finish = step.get("finish", finish)
        return LLMResult(
            message=message,
            text=visible_text(message),
            tool_calls=extract_tool_calls(
                message, finish, [t["function"]["name"] for t in tools or []] or TOOL_NAMES
            ),
            finish_reason=finish,
            timings={"prompt_tokens": 100, "predicted_n": 10},
            ttft_ms=1.0,
            total_ms=float(step.get("ms", 10)),
            queued_ms=0.0,
            attempts=1,
            model="scripted",
        )


def make_stack(
    tmp_path, ha=None, llm=None, agents: list[dict] | None = None, settings=None
) -> SimpleNamespace:
    """The agent stack wired like app.main.create_app, on a stub HA and an in-memory database."""
    ha = ha or StubHA()
    llm = llm or ScriptedLLM()
    store = Store(":memory:")
    feed = Feed(store)
    safety = Safety(store, feed, tmp_path)
    agent_store = AgentStore(tmp_path)
    for config in [AGENT] if agents is None else agents:
        agent_store.save(parse_agent(config))
    runtime = AgentRuntime(
        agents=agent_store, ha=ha, llm=llm, store=store, feed=feed, safety=safety, runtime_settings=settings
    )
    council = Council(store, feed, ha, agent_store, safety, settings)
    actuator = Actuator(
        store, feed, ha, safety, agent_store, runtime.scope, owner_of=council.ownership.owner_of,
        pseudo=council.agent_config,
    )  # fmt: skip
    gate = EventGate(agent_store, feed, store, runtime.scope, runtime.wake)
    runtime.actuator, runtime.gate, runtime.council = actuator, gate, council
    council.rt, council.actuator, council.gate = runtime, actuator, gate
    return SimpleNamespace(
        ha=ha, llm=llm, store=store, feed=feed, safety=safety, agents=agent_store, rt=runtime,
        actuator=actuator, gate=gate, council=council,
    )  # fmt: skip


def go_live(stack, *agent_ids: str) -> None:
    stack.safety.set_mode("live")
    for agent_id in agent_ids or list(stack.agents.agents):
        stack.agents.save(stack.agents.get(agent_id).model_copy(update={"mode": "live"}))


async def settle(stack) -> None:
    """Wait until every queued run has finished."""
    while stack.rt._tasks:
        await asyncio.gather(*list(stack.rt._tasks), return_exceptions=True)
        await asyncio.sleep(0)  # gather over finished tasks does not yield; their callbacks must run


async def run(stack, agent_id: str = "room", reason: str = "test wake", source: str = "manual") -> dict:
    run_id = stack.rt.wake(agent_id, source=source, reason=reason)
    await settle(stack)
    return stack.rt.get_run(run_id)


def steps(run: dict, kind: str) -> list[dict]:
    return [s["data"] for s in run["trace"] if s["type"] == kind]


class AutomationHA(StubHA):
    """StubHA plus the automation config API, with the HA client's own id guard and
    `sim/fake_ha.py`'s `validate_config` rules."""

    connected = True

    def __init__(self, entities: list[dict] | None = None, **kwargs) -> None:
        super().__init__(entities, **kwargs)
        self.automations: dict[str, dict] = {"1700000000001": {"id": "1700000000001", "alias": "Owner's own"}}
        self.writes: list[tuple] = []
        self.history_rows: dict[str, list[dict]] = {}
        self.logbook_rows: list[dict] = []

    async def validate_automation(self, config: dict) -> dict:
        from sim.fake_ha import HAState

        parts = {k: config[k] for k in ("triggers", "conditions", "actions") if config.get(k)}
        return {k: HAState.validate_part(k, v) for k, v in parts.items()}

    async def get_automation(self, ha_id: str):
        return self.automations.get(ha_id)

    async def write_automation(self, ha_id: str, config: dict) -> None:
        from app.ha.client import guard_ours

        guard_ours(ha_id)
        if self.fail:
            raise HAError(self.fail)
        self.writes.append(("write", ha_id, config))
        self.automations[ha_id] = {**config, "id": ha_id}
        self.states[f"automation.{ha_id}"] = {"entity_id": f"automation.{ha_id}", "state": "on",
                                              "attributes": {"id": ha_id}}  # fmt: skip

    async def delete_automation(self, ha_id: str) -> None:
        from app.ha.client import guard_ours

        guard_ours(ha_id)
        self.writes.append(("delete", ha_id, None))
        self.automations.pop(ha_id, None)
        self.states.pop(f"automation.{ha_id}", None)

    def automation_entity(self, ha_id: str):
        entity = f"automation.{ha_id}"
        return entity if entity in self.states else None

    async def set_automation_enabled(self, ha_id: str, enabled: bool) -> None:
        from app.ha.client import guard_ours

        guard_ours(ha_id)
        self.writes.append(("enable" if enabled else "disable", ha_id, None))
        self.states[f"automation.{ha_id}"]["state"] = "on" if enabled else "off"

    async def history(self, entity_id, start, end) -> list[dict]:
        return self.history_rows.get(entity_id, [])

    async def logbook(self, start: str, end: str) -> list[dict]:
        from datetime import datetime

        lo, hi = datetime.fromisoformat(start).timestamp(), datetime.fromisoformat(end).timestamp()
        return [r for r in self.logbook_rows if lo <= datetime.fromisoformat(r["when"]).timestamp() < hi]
