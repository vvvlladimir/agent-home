"""Home Assistant client: REST for one-shot reads, WebSocket for registries and live events.

Read-only except two write paths:
- `call_service`, whose only caller is `core/actuator.py` (after the policy decision;
  tests/unit/test_actuator.py greps for other callers);
- automation writes (`write_automation`, `delete_automation`, `set_automation_enabled`), whose only
  caller is `core/lifecycle.py`. Each refuses any id that is not `agent_house_<slug>`: the owner's
  own automations can never be written or deleted from here.
- `set_original_enabled` (M5 migration, only caller `core/migration.py`): `automation.turn_off` /
  `turn_on` on one of the owner's automations, on the owner's explicit click. Never an edit.

API shapes: docs/RESEARCH.md §3. The registry list responses and the Supervisor proxy
behaviour are [UNVERIFIED-ON-HW]; `tests/integration/test_real_ha.py` checks them against a
real Home Assistant Core when run with `make test-realha`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import httpx
import websockets

from ..config import Settings
from .registry import Registry

log = logging.getLogger(__name__)

REGISTRY_EVENTS = ("area_registry_updated", "device_registry_updated", "entity_registry_updated")
# The WebSocket commands `command()` accepts. `call_service` is deliberately absent: it has its
# own method below, for the actuator only.
READ_COMMANDS = frozenset(
    {
        "get_states",
        "get_config",
        "subscribe_events",
        "config/area_registry/list",
        "config/device_registry/list",
        "config/entity_registry/list",
        "validate_config",
    }
)


AUTOMATION_PREFIX = "agent_house_"
OUR_AUTOMATION_ID = re.compile(r"^agent_house_[a-z0-9_]{1,64}$")
OWNER_AUTOMATION = re.compile(r"^automation\.[a-z0-9_]+$")


class HAError(Exception):
    pass


class NotOurs(HAError):
    """A write was asked for an automation Agent House did not create."""


def guard_ours(ha_id: object) -> str:
    if not isinstance(ha_id, str) or not OUR_AUTOMATION_ID.fullmatch(ha_id):
        raise NotOurs(f"refused: {ha_id!r} is not an {AUTOMATION_PREFIX}<slug> automation")
    return ha_id


class HAClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = "connecting"  # connecting | connected | auth_failed | unreachable
        self.last_error: str | None = None
        self.version: str | None = None
        self.location_name: str | None = None
        self.reconnects = 0
        self.last_event_ts: float | None = None
        self.states: dict[str, dict] = {}
        self.registry = Registry()
        self._listeners: list[Callable[[str, dict | None, dict | None], Any]] = []
        self._event_listeners: dict[str, list[Callable[[dict], Any]]] = {}
        self._http = httpx.AsyncClient(
            base_url=settings.ha_rest_url,
            headers={"Authorization": f"Bearer {settings.ha_token}"},
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
        self._ws: Any = None
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._subscriptions: dict[int, str] = {}
        self._task: asyncio.Task | None = None
        self._registry_refresh: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._was_connected = False

    # --- lifecycle -----------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self.state == "connected"

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="ha-client")

    async def stop(self) -> None:
        for task in (self._task, self._registry_refresh):
            if task:
                task.cancel()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
        await self._http.aclose()

    async def wait_ready(self, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
            return True
        except TimeoutError:
            return False

    def on_state_changed(self, callback: Callable[[str, dict | None, dict | None], Any]) -> None:
        """callback(entity_id, old_state, new_state); may be sync or async."""
        self._listeners.append(callback)

    def on_event(self, event_type: str, callback: Callable[[dict], Any]) -> None:
        """Subscribe to another HA event type (takes effect on the next (re)connect if already connected)."""
        self._event_listeners.setdefault(event_type, []).append(callback)

    # --- connection loop -------------------------------------------------------------
    async def _run(self) -> None:
        backoff = 1.0
        while True:
            self._was_connected = False
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except _AuthFailed as err:
                self.state, self.last_error = "auth_failed", str(err)
                log.error("Home Assistant rejected the token: %s", err)
                backoff = 30.0  # a wrong token does not fix itself quickly
            except Exception as err:  # noqa: BLE001 - any failure means: reconnect
                self.state, self.last_error = "unreachable", f"{type(err).__name__}: {err}"
                log.warning("Home Assistant connection lost: %s", self.last_error)
            finally:
                self._ready.clear()
                self._fail_pending("connection closed")
            self.reconnects += 1
            if self._was_connected:
                backoff = 1.0  # it worked until just now: retry quickly, then back off again
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _session(self) -> None:
        url = self.settings.ha_websocket_url
        async with websockets.connect(url, max_size=None, open_timeout=10, ping_interval=30) as ws:
            self._ws = ws
            await self._authenticate(ws)
            reader = asyncio.create_task(self._reader(ws), name="ha-ws-reader")
            try:
                await self._load_config()
                await self.refresh_registry()
                # Subscribe before the snapshot so no change falls into the gap between the two;
                # an event that arrives early is simply overwritten by the (newer) snapshot.
                for event_type in ("state_changed", *REGISTRY_EVENTS, *self._event_listeners):
                    await self.command("subscribe_events", event_type=event_type)
                # Replace, do not merge: entities removed while we were away must disappear.
                self.states = {s["entity_id"]: s for s in await self.command("get_states")}
                self.state, self.last_error = "connected", None
                self._was_connected = True
                self._ready.set()
                log.info(
                    "connected to Home Assistant %s (%d areas, %d entities)",
                    self.version,
                    len(self.registry.areas),
                    len(self.states),
                )
                await reader
            finally:
                reader.cancel()
                self._ws = None

    async def _authenticate(self, ws: Any) -> None:
        hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if hello.get("type") != "auth_required":
            raise HAError(f"unexpected greeting: {hello.get('type')}")
        await ws.send(json.dumps({"type": "auth", "access_token": self.settings.ha_token}))
        answer = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if answer.get("type") != "auth_ok":
            raise _AuthFailed(answer.get("message") or answer.get("type") or "auth failed")
        self.version = answer.get("ha_version") or hello.get("ha_version")

    async def _load_config(self) -> None:
        try:
            config = await self.command("get_config")
            self.version = config.get("version") or self.version
            self.location_name = config.get("location_name")
        except HAError as err:  # not fatal: only cosmetic fields
            log.debug("get_config failed: %s", err)

    async def refresh_registry(self) -> None:
        areas, devices, entities = await asyncio.gather(
            self.command("config/area_registry/list"),
            self.command("config/device_registry/list"),
            self.command("config/entity_registry/list"),
        )
        self.registry = Registry.build(areas, devices, entities)

    async def _reader(self, ws: Any) -> None:
        try:
            await self._read(ws)
        finally:
            self._fail_pending("connection closed")  # do not let callers wait out their timeout

    async def _read(self, ws: Any) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "result":
                future = self._pending.pop(msg.get("id"), None)
                if future and not future.done():
                    if msg.get("success"):
                        future.set_result(msg.get("result"))
                    else:
                        error = msg.get("error") or {}
                        future.set_exception(HAError(f"{error.get('code')}: {error.get('message')}"))
            elif kind == "event":
                await self._handle_event(msg.get("event") or {})
        raise HAError("websocket closed by Home Assistant")

    async def _handle_event(self, event: dict) -> None:
        event_type = event.get("event_type")
        data = event.get("data") or {}
        self.last_event_ts = time.time()
        if event_type == "state_changed":
            entity_id, new, old = data.get("entity_id"), data.get("new_state"), data.get("old_state")
            if not entity_id:
                return
            if new is None:
                self.states.pop(entity_id, None)
            else:
                self.states[entity_id] = new
            await self._dispatch(self._listeners, entity_id, old, new)
        elif event_type in REGISTRY_EVENTS:
            if not self._registry_refresh or self._registry_refresh.done():
                self._registry_refresh = asyncio.create_task(self._refresh_registry_soon())
        if event_type in self._event_listeners:
            await self._dispatch(self._event_listeners[event_type], event)

    async def _refresh_registry_soon(self) -> None:
        await asyncio.sleep(2)  # registry edits come in bursts
        try:
            await self.refresh_registry()
        except Exception as err:  # noqa: BLE001
            log.warning("registry refresh failed: %s", err)

    @staticmethod
    async def _dispatch(callbacks: list[Callable], *args: Any) -> None:
        for callback in callbacks:
            try:
                result = callback(*args)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - a broken listener must not kill the connection
                log.exception("listener failed")

    # --- websocket commands ----------------------------------------------------------
    async def command(self, type_: str, timeout: float = 20.0, **payload: Any) -> Any:
        """Send a read-only WebSocket command. Never await this from inside an event listener:
        the listener runs on the reader task, which is the one that would deliver the answer."""
        if type_ not in READ_COMMANDS:
            raise HAError(f"command {type_!r} is not on the read-only allowlist")
        return await self._send(type_, timeout, **payload)

    async def _send(self, type_: str, timeout: float = 20.0, **payload: Any) -> Any:
        ws = self._ws
        if ws is None:
            raise HAError("not connected")
        self._msg_id += 1
        msg_id = self._msg_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            await ws.send(json.dumps({"id": msg_id, "type": type_, **payload}))
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as err:
            raise HAError(f"timeout waiting for {type_}") from err
        finally:
            self._pending.pop(msg_id, None)

    async def call_service(self, domain: str, service: str, entity_id: str, data: dict | None = None) -> None:
        """Actuate ONE entity. Only `core/actuator.py` may call this, after `policy.decide`."""
        if not isinstance(entity_id, str) or not entity_id:
            raise HAError("call_service needs exactly one entity_id")
        await self._send(
            "call_service",
            domain=domain,
            service=service,
            service_data=dict(data or {}),
            target={"entity_id": entity_id},
        )

    # --- automation writes (core/lifecycle.py only) -------------------------------------
    async def _rest(self, method: str, path: str, body: Any = None) -> Any:
        try:
            response = await self._http.request(method, path, json=body)
        except httpx.HTTPError as err:
            raise HAError(f"{type(err).__name__}: {err}") from err
        if response.status_code >= 400:
            try:
                detail = response.json().get("message")
            except ValueError:
                detail = response.text[:200]
            raise HAError(f"HTTP {response.status_code} for {method} {path}: {detail}")
        return response.json() if response.content else None

    async def write_automation(self, ha_id: str, config: dict) -> None:
        """Create or replace one of OUR automations; HA reloads it by itself (RESEARCH §3)."""
        guard_ours(ha_id)
        if not isinstance(config, dict):
            raise HAError("an automation config must be an object")
        await self._rest("POST", f"/config/automation/config/{ha_id}", {**config, "id": ha_id})

    async def delete_automation(self, ha_id: str) -> None:
        guard_ours(ha_id)
        await self._rest("DELETE", f"/config/automation/config/{ha_id}")

    async def get_automation(self, ha_id: str) -> dict | None:
        """Config of any automation by its config id (read-only); None when HA does not know it."""
        try:
            return await self.rest_get(f"/config/automation/config/{ha_id}")
        except HAError as err:
            if "HTTP 404" in str(err):
                return None
            raise

    def automation_entity(self, ha_id: str) -> str | None:
        """automation.<alias slug> whose `attributes.id` is the config id."""
        for entity_id, state in self.states.items():
            if entity_id.startswith("automation.") and (state.get("attributes") or {}).get("id") == ha_id:
                return entity_id
        return None

    async def set_automation_enabled(self, ha_id: str, enabled: bool) -> None:
        guard_ours(ha_id)
        entity_id = self.automation_entity(ha_id)
        if entity_id is None:
            raise HAError(f"{ha_id} has no automation entity in Home Assistant")
        await self._send(
            "call_service",
            domain="automation",
            service="turn_on" if enabled else "turn_off",
            service_data={},
            target={"entity_id": entity_id},
        )

    async def set_original_enabled(self, entity_id: str, enabled: bool) -> None:
        """Switch one of the OWNER's automations off or on (`automation.turn_off` / `turn_on`): the
        reversible "disable original" of the migration page, on the owner's explicit click. Its only
        caller is `core/migration.py`. Nothing is edited or deleted; ours go through the lifecycle."""
        state = self.states.get(entity_id) if isinstance(entity_id, str) else None
        if state is None or not OWNER_AUTOMATION.fullmatch(entity_id):
            raise HAError(f"{entity_id!r} is not an automation Home Assistant knows")
        if str((state.get("attributes") or {}).get("id") or "").startswith(AUTOMATION_PREFIX):
            raise HAError(f"{entity_id} is one of ours: use the Deployed automations page")
        await self._send(
            "call_service",
            domain="automation",
            service="turn_on" if enabled else "turn_off",
            service_data={},
            target={"entity_id": entity_id},
        )

    async def get_script(self, object_id: str) -> dict | None:
        """Config of a UI-managed script (read-only); None when HA has no editable config for it.
        The path is [UNVERIFIED-ON-HW] (docs/OWNER_CHECKLIST.md M5)."""
        try:
            return await self.rest_get(f"/config/script/config/{object_id}")
        except HAError as err:
            if "HTTP 404" in str(err):
                return None
            raise

    async def validate_automation(self, config: dict) -> dict:
        """WebSocket `validate_config` with the plural keys → {"triggers": {"valid", "error"}, …}."""
        parts = {k: config[k] for k in ("triggers", "conditions", "actions") if config.get(k)}
        return await self.command("validate_config", **parts)

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(HAError(reason))
        self._pending.clear()

    # --- REST (read-only) --------------------------------------------------------------
    async def rest_get(self, path: str, **params: Any) -> Any:
        try:
            response = await self._http.get(path, params=params or None)
        except httpx.HTTPError as err:
            raise HAError(f"{type(err).__name__}: {err}") from err
        if response.status_code >= 400:
            raise HAError(f"HTTP {response.status_code} for GET {path}: {response.text[:200]}")
        return response.json()

    async def history(self, entity_id: str, start: str, end: str) -> list[dict]:
        """State changes of one entity between two ISO timestamps, oldest first.
        The flag parameters and the path timestamp are [UNVERIFIED-ON-HW] (docs/RESEARCH.md §3)."""
        rows = await self.rest_get(
            f"/history/period/{start}",
            filter_entity_id=entity_id,
            end_time=end,
            minimal_response="",
            no_attributes="",
        )
        return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], list) else []

    async def api_alive(self) -> bool:
        """GET /api/ → {"message": "API running."}; used by diagnostics to tell REST from WS problems."""
        try:
            return "message" in await self.rest_get("/")
        except HAError:
            return False

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "state": self.state,
            "via_supervisor": self.settings.via_supervisor,
            "version": self.version,
            "location_name": self.location_name,
            "last_error": self.last_error,
            "last_event_ts": self.last_event_ts,
            "reconnects": self.reconnects,
            "areas": len(self.registry.areas),
            "entities": len(self.states),
        }


class _AuthFailed(HAError):
    pass
