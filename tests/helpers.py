"""In-process stand-ins for unit tests of the safety layer and its actors: a stub Home Assistant and
the wiring the application factory does (council, actuator, the orchestrator as house manager)."""

from __future__ import annotations

from types import SimpleNamespace

from app.config import RuntimeSettings
from app.core.actuator import Actuator
from app.core.council import LEARNER, ORCHESTRATOR, Council
from app.core.inbox import Inbox
from app.core.manager import Manager
from app.core.orchestrator import Orchestrator, ProviderError, Turn
from app.core.safety import Safety
from app.ha.client import HAError
from app.ha.registry import Registry
from app.store.db import Store
from app.store.feed import Feed

ROOM = [
    {"entity_id": "light.room_ceiling", "state": "off"},
    {"entity_id": "light.room_lamp", "state": "off"},
    {"entity_id": "scene.room_movie", "state": "scening"},
    {"entity_id": "climate.room", "state": "heat", "attributes": {"temperature": 21}},
    {"entity_id": "switch.room_heater", "state": "off"},
    {"entity_id": "humidifier.room", "state": "off"},
    {"entity_id": "lock.room_door", "state": "locked"},
    {"entity_id": "binary_sensor.room_motion", "state": "off"},
    {"entity_id": "sensor.room_illuminance", "state": "4", "attributes": {"unit_of_measurement": "lx"}},
]


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


def cloud_settings(**over) -> SimpleNamespace:
    """The add-on options the orchestrator reads; no key, so no cloud call can leave a test."""
    base = dict(orchestrator_provider="auto", anthropic_api_key="", anthropic_model="claude-haiku-5-5",
                anthropic_base_url="", openai_api_key="", openai_model="gpt-6.1-sol", openai_base_url="",
                daily_budget_eur=1.0, redact_cloud=True)  # fmt: skip
    return SimpleNamespace(**{**base, **over})


class FakeLearner:
    """What the council asks of core/learner.py: configured, and the live targets of a fresh model."""

    configured = True

    def __init__(self, targets: list[str] | None = None) -> None:
        self._targets = list(targets or [])

    def targets(self) -> list[str]:
        return list(self._targets)


def call(name: str, **arguments) -> dict:
    """One scripted model turn with one tool call."""
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


class ScriptedProvider:
    """A cloud provider of the orchestrator that answers from a list of steps (`{"text": ...}`,
    `call(...)`, `{"raise": "why"}`), then says "Done."; it records what it was sent."""

    def __init__(self, steps: list[dict] | None = None, name: str = "anthropic") -> None:
        self.name, self.steps, self.requests = name, list(steps or []), []
        self.has_key = self.available = True

    async def turn(self, system: str, transcript: list[dict], tools: list[dict]) -> Turn:
        self.requests.append({"system": system, "transcript": transcript, "tools": [t["name"] for t in tools]})
        step = self.steps.pop(0) if self.steps else {"text": "Done."}
        if step.get("raise"):
            raise ProviderError(step["raise"])
        calls = [{"id": f"c{len(self.requests)}_{i}", "name": c["name"], "input": c.get("arguments") or {}}
                 for i, c in enumerate(step.get("tool_calls") or [])]  # fmt: skip
        return Turn(text=step.get("text", ""), calls=calls, stop_reason="tool_use" if calls else "end_turn",
                    tokens_in=100, tokens_out=10, provider=self.name, model="scripted")  # fmt: skip


def make_stack(tmp_path, ha=None, settings: RuntimeSettings | None = None, learner=None) -> SimpleNamespace:
    """The actors wired like app.main.create_app, on a stub HA and an in-memory database. The house
    manager is on, so the orchestrator may act on the switchable devices (still in shadow)."""
    ha = ha or StubHA()
    settings = settings or RuntimeSettings(manager_enabled=True)
    store = Store(":memory:")
    feed = Feed(store)
    safety = Safety(store, feed, tmp_path)
    inbox = Inbox(store, feed)
    council = Council(store, feed, ha, safety, settings)
    actuator = Actuator(
        store, feed, ha, safety, council.actor, owner_of=council.ownership.owner_of,
        delegates={LEARNER: ORCHESTRATOR},
    )  # fmt: skip
    council.actuator = actuator
    orch = Orchestrator(cloud_settings(), store, feed, ha, safety, None, inbox)
    orch.council = council
    council.manager = Manager(orch, council, actuator, None, settings, inbox)
    council.learner = learner
    return SimpleNamespace(
        ha=ha, store=store, feed=feed, safety=safety, inbox=inbox, council=council, actuator=actuator,
        orch=orch, manager=council.manager, settings=settings,
    )  # fmt: skip


def go_live(stack, *actors: str) -> None:
    """The global mode and the actors' own modes live (default: the orchestrator)."""
    stack.safety.set_mode("live")
    for actor in actors or (ORCHESTRATOR,):
        key = "learner_mode" if actor == LEARNER else "orchestrator_mode"
        stack.settings = stack.settings.model_copy(update={key: "live"})
    stack.council.settings = stack.manager.settings = stack.settings


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
