"""Fake Home Assistant Core: the REST + WebSocket subset Agent House uses."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse

ALLOWED_AUTOMATION_KEYS = {
    "id",
    "alias",
    "description",
    "triggers",
    "trigger",
    "conditions",
    "condition",
    "actions",
    "action",
    "mode",
    "max",
    "max_exceeded",
    "variables",
    "trigger_variables",
    "initial_state",
}
ONOFF = {"light", "switch", "fan", "input_boolean", "media_player"}


class HAError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


def _dt(ts: Any = None) -> datetime:
    if ts is None:
        return datetime.now(UTC)
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    if isinstance(ts, int | float):
        return datetime.fromtimestamp(ts, UTC)
    d = datetime.fromisoformat(str(ts).replace(" ", "+").replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "automation"


def _ctx() -> dict:
    return {"id": uuid.uuid4().hex[:26].upper(), "parent_id": None, "user_id": None}


class _Conn:
    def __init__(self, ws: WebSocket):
        self.ws, self.subs, self.q = ws, {}, asyncio.Queue()


class HAState:
    def __init__(self, populate: bool = False, token: str = "sim-token"):
        self.token, self.version = token, "2026.11.1"
        self.loop: asyncio.AbstractEventLoop | None = None
        self._conns: set[_Conn] = set()
        self._populate_on_reset = populate
        self._init()

    @classmethod
    def default(cls) -> HAState:
        return cls(populate=True)

    def _init(self) -> None:
        self.areas, self.devices, self.entity_registry = [], [], []
        self.states: dict[str, dict] = {}
        self.automations: dict[str, dict] = {}
        self.service_calls: list[dict] = []
        self.config_writes: list[dict] = []
        self.history: dict[str, list[dict]] = {}
        self._fail, self._fail_status = 0, 500
        if self._populate_on_reset:
            self._populate()

    def reset(self) -> None:
        self._init()

    def fail_next(self, n: int = 1, status: int = 500) -> None:
        self._fail, self._fail_status = n, status

    # ---- state + events -------------------------------------------------------------------
    def set_state(self, entity_id: str, state: str, attributes: dict | None = None, ts: Any = None) -> dict:
        now = _dt(ts)
        old = self.states.get(entity_id)
        attrs = dict(attributes) if attributes is not None else dict(old["attributes"] if old else {})
        changed = old["last_changed"] if old and old["state"] == str(state) else now.isoformat()
        new = {
            "entity_id": entity_id,
            "state": str(state),
            "attributes": attrs,
            "last_changed": changed,
            "last_updated": now.isoformat(),
            "context": _ctx(),
        }
        if old:
            self.history.setdefault(entity_id, []).append(old)
        self.states[entity_id] = new
        self.fire_event("state_changed", {"entity_id": entity_id, "old_state": old, "new_state": new})
        return new

    def remove_state(self, entity_id: str) -> None:
        old = self.states.pop(entity_id, None)
        if old:
            self.fire_event("state_changed", {"entity_id": entity_id, "old_state": old, "new_state": None})

    def fire_event(self, event_type: str, data: dict | None = None) -> None:
        ev = {
            "event_type": event_type,
            "data": data or {},
            "origin": "LOCAL",
            "time_fired": _dt().isoformat(),
            "context": _ctx(),
        }
        loop = self.loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._dispatch, ev)
        else:
            self._dispatch(ev)

    def _dispatch(self, ev: dict) -> None:
        for c in list(self._conns):
            for sid, et in c.subs.items():
                if et in (None, ev["event_type"]):
                    c.q.put_nowait({"id": sid, "type": "event", "event": ev})

    def drop_ws(self) -> None:
        async def _close():
            for c in list(self._conns):
                with contextlib.suppress(Exception):
                    await c.ws.close(code=1012)

        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(_close(), self.loop)

    # ---- services -------------------------------------------------------------------------
    def call_service(self, domain: str, service: str, data: dict) -> list[dict]:
        data = dict(data)
        ids = data.get("entity_id") or []
        ids = [ids] if isinstance(ids, str) else list(ids)
        changed: list[dict] = []

        def upd(eid: str, state: str | None = None, **attrs: Any) -> None:
            cur = self.states.get(eid)
            if cur:
                changed.append(self.set_state(eid, state or cur["state"], {**cur["attributes"], **attrs}))

        key = (domain, service)
        if service in ("turn_on", "turn_off", "toggle") and (
            domain in ONOFF or domain in ("homeassistant", "automation")
        ):
            for eid in ids:
                cur = self.states.get(eid)
                if not cur:
                    continue
                dom = eid.split(".")[0]
                on = service == "turn_on" or (
                    service == "toggle" and cur["state"] in ("off", "idle", "standby")
                )
                attrs: dict = {}
                if dom == "light":
                    if on and "brightness_pct" in data:
                        attrs["brightness"] = round(data["brightness_pct"] * 255 / 100)
                    elif on and "brightness" in data:
                        attrs["brightness"] = data["brightness"]
                    elif not on:
                        attrs["brightness"] = None
                upd(eid, "on" if on else "off", **attrs)
        elif key == ("climate", "set_temperature"):
            for eid in ids:
                upd(eid, **{"temperature": data.get("temperature")})
        elif key == ("climate", "set_hvac_mode"):
            for eid in ids:
                upd(eid, data.get("hvac_mode", "off"))
        elif key in (("cover", "open_cover"), ("cover", "close_cover")):
            op = service == "open_cover"
            for eid in ids:
                upd(eid, "open" if op else "closed", current_position=100 if op else 0)
        elif key == ("cover", "set_cover_position"):
            pos = int(data.get("position", 0))
            for eid in ids:
                upd(eid, "open" if pos > 0 else "closed", current_position=pos)
        elif domain == "lock" and service in ("lock", "unlock"):
            for eid in ids:
                upd(eid, "locked" if service == "lock" else "unlocked")
        elif key == ("automation", "trigger"):
            for eid in ids:
                upd(eid, last_triggered=_dt().isoformat())
        elif key == ("automation", "reload"):
            pass
        elif domain == "alarm_control_panel" and service in (
            "alarm_disarm",
            "alarm_arm_home",
            "alarm_arm_away",
        ):
            for eid in ids:
                upd(eid, service.replace("alarm_", "armed_") if "arm_" in service else "disarmed")
        else:
            raise HAError(400, "Service not found.")
        self.service_calls.append({"domain": domain, "service": service, "data": data, "ts": time.time()})
        return changed

    # ---- automations ----------------------------------------------------------------------
    def put_automation(self, cid: str, config: dict) -> None:
        cfg = {**config, "id": cid}
        self.automations[cid] = cfg
        eid = f"automation.{slugify(cfg.get('alias') or cid)}"
        for old in [e for e, s in self.states.items() if s["attributes"].get("id") == cid and e != eid]:
            self.remove_state(old)
        prev = self.states.get(eid, {}).get("attributes", {})
        self.set_state(
            eid,
            "on",
            {
                "id": cid,
                "friendly_name": cfg.get("alias", cid),
                "last_triggered": prev.get("last_triggered"),
                "mode": cfg.get("mode", "single"),
                "current": 0,
            },
        )

    def delete_automation(self, cid: str) -> None:
        self.automations.pop(cid)
        for e in [e for e, s in self.states.items() if s["attributes"].get("id") == cid]:
            self.remove_state(e)

    @staticmethod
    def validate_automation(cfg: Any) -> None:
        if not isinstance(cfg, dict):
            raise HAError(400, "Message malformed: expected a dictionary")
        for k in cfg:
            if k not in ALLOWED_AUTOMATION_KEYS:
                raise HAError(400, f"Message malformed: extra keys not allowed @ data['{k}']")
        for need in (("triggers", "trigger"), ("actions", "action")):
            if not any(k in cfg for k in need):
                raise HAError(400, f"Message malformed: required key not provided @ data['{need[0]}']")

    TRIGGER_PLATFORMS = frozenset(
        {"state", "numeric_state", "time", "time_pattern", "sun", "event", "homeassistant", "template",
         "zone", "device", "mqtt", "webhook", "calendar", "tag", "conversation"}
    )  # fmt: skip
    ACTION_KEYS = frozenset(
        {"action", "service", "delay", "condition", "choose", "if", "sequence", "parallel", "repeat",
         "wait_template", "wait_for_trigger", "event", "scene", "stop", "variables"}
    )  # fmt: skip

    @classmethod
    def validate_part(cls, kind: str, value: Any) -> dict:
        """One key of the WebSocket `validate_config` command (plural keys, like real HA)."""
        items = value if isinstance(value, list) else [value]
        if not items or any(not isinstance(i, dict) or not i for i in items):
            return {"valid": False, "error": f"{kind}: expected a non-empty object or list of objects"}
        for item in items:
            if kind == "triggers":
                platform = item.get("trigger", item.get("platform"))
                if platform is None:
                    return {"valid": False, "error": "required key not provided @ data['trigger']"}
                if platform not in cls.TRIGGER_PLATFORMS:
                    return {"valid": False, "error": f"Invalid trigger '{platform}' specified"}
            elif kind == "conditions" and "condition" not in item:
                return {"valid": False, "error": "required key not provided @ data['condition']"}
            elif kind == "actions" and not set(item) & cls.ACTION_KEYS:
                return {"valid": False, "error": f"Unable to determine action @ data[0]: {sorted(item)}"}
        return {"valid": True, "error": None}

    # ---- history --------------------------------------------------------------------------
    def history_for(self, ids: list[str], start: datetime, end: datetime) -> list[list[dict]]:
        """Like real HA: each list starts with the state the entity had at `start` (its
        `last_changed` moved to `start`), then every change up to `end`."""
        out = []
        for eid in ids:
            rows = self.history.get(eid, []) + ([self.states[eid]] if eid in self.states else [])
            before = [r for r in rows if _dt(r["last_changed"]) < start]
            inside = [r for r in rows if start <= _dt(r["last_changed"]) <= end]
            if before:
                inside.insert(
                    0, {**before[-1], "last_changed": start.isoformat(), "last_updated": start.isoformat()}
                )
            if inside:
                out.append(inside)
        return out

    # ---- registries + info ----------------------------------------------------------------
    def config(self) -> dict:
        return {
            "version": self.version,
            "location_name": "Sim Home",
            "time_zone": "Europe/Berlin",
            "unit_system": {"length": "km", "temperature": "°C", "mass": "g", "volume": "L"},
            "state": "RUNNING",
            "components": ["api", "websocket", "light", "sensor", "automation", "history"],
        }

    def _populate(self) -> None:
        self.areas = [
            {
                "area_id": a,
                "name": n,
                "floor_id": None,
                "aliases": [],
                "icon": None,
                "labels": [],
                "picture": None,
            }
            for a, n in [
                ("kitchen", "Kitchen"),
                ("living_room", "Living Room"),
                ("bedroom", "Bedroom"),
                ("hallway", "Hallway"),
            ]
        ]
        n = [0]

        def add(eid: str, state: str, attrs: dict, area: str | None = None, dev: bool = False) -> None:
            dom, obj = eid.split(".")
            name = attrs.get("friendly_name") or obj.replace("_", " ").title()
            dev_id = None
            if dev:
                n[0] += 1
                dev_id = f"dev{n[0]:03d}"
                self.devices.append(
                    {
                        "id": dev_id,
                        "area_id": area,
                        "name": name,
                        "name_by_user": None,
                        "manufacturer": "SimCo",
                        "model": f"{dom.title()} v1",
                        "disabled_by": None,
                    }
                )
            self.entity_registry.append(
                {
                    "entity_id": eid,
                    "area_id": None if dev else area,
                    "device_id": dev_id,
                    "platform": "sim",
                    "name": None,
                    "original_name": name,
                    "disabled_by": None,
                    "hidden_by": None,
                    "entity_category": None,
                    "unique_id": f"sim-{obj}",
                    "labels": [],
                    "id": uuid.uuid4().hex,
                }
            )
            self.set_state(
                eid, state, {"friendly_name": name, **attrs}, ts=datetime.now(UTC) - timedelta(hours=1)
            )

        light = {"supported_color_modes": ["brightness"], "color_mode": "brightness"}
        temp = {"device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement"}
        hum = {"device_class": "humidity", "unit_of_measurement": "%", "state_class": "measurement"}
        clim = {
            "hvac_modes": ["off", "heat", "cool", "auto"],
            "min_temp": 7,
            "max_temp": 35,
            "hvac_action": "idle",
        }
        blind = {"device_class": "blind", "current_position": 100}
        k, lr, b, h = "kitchen", "living_room", "bedroom", "hallway"
        add("light.kitchen_ceiling", "on", {**light, "brightness": 200}, k)
        add("light.kitchen_counter", "off", {**light, "brightness": None}, k)
        add("sensor.kitchen_temperature", "21.4", temp, k, dev=True)
        add("binary_sensor.kitchen_window", "off", {"device_class": "window"}, k, dev=True)
        add("light.living_room_lamp", "on", {**light, "brightness": 150}, lr)
        add("media_player.living_room_tv", "off", {"device_class": "tv", "volume_level": 0.3}, lr)
        add("fan.living_room_fan", "off", {"percentage": 0, "preset_modes": ["auto"]}, lr)
        add("climate.living_room", "heat", {**clim, "current_temperature": 20.8, "temperature": 21}, lr)
        add("cover.living_room_blinds", "open", blind, lr)
        add("sensor.living_room_temperature", "20.8", temp, lr, dev=True)
        add("sensor.living_room_humidity", "47", hum, lr, dev=True)
        add(
            "sensor.living_room_illuminance",
            "120",
            {"device_class": "illuminance", "unit_of_measurement": "lx"},
            lr,
            dev=True,
        )
        add(
            "sensor.living_room_power",
            "85.2",
            {"device_class": "power", "unit_of_measurement": "W", "state_class": "measurement"},
            lr,
            dev=True,
        )
        add("binary_sensor.living_room_motion", "off", {"device_class": "motion"}, lr, dev=True)
        add("light.bedroom_lamp", "off", {**light, "brightness": None}, b)
        add("climate.bedroom", "heat", {**clim, "current_temperature": 18.9, "temperature": 19}, b)
        add("cover.bedroom_blinds", "closed", {**blind, "current_position": 0}, b)
        add("sensor.bedroom_temperature", "18.9", temp, b, dev=True)
        add("sensor.bedroom_humidity", "52", hum, b, dev=True)
        add("switch.space_heater", "off", {"device_class": "outlet"}, b)
        add("binary_sensor.bedroom_window", "off", {"device_class": "window"}, b, dev=True)
        add("light.hallway", "off", {**light, "brightness": None}, h)
        add("binary_sensor.hallway_motion", "off", {"device_class": "motion"}, h, dev=True)
        add("binary_sensor.front_door_contact", "off", {"device_class": "door"}, h, dev=True)
        add("lock.front_door", "locked", {}, h)
        add("camera.hallway", "idle", {"brand": "SimCam"}, h)
        add("alarm_control_panel.home", "disarmed", {"code_arm_required": False}, h)
        add("cover.garage_door", "closed", {"device_class": "garage", "current_position": 0}, h)
        add("person.owner", "home", {"user_id": "sim-owner", "latitude": 52.5, "longitude": 13.4})
        add("sun.sun", "above_horizon", {"elevation": 12.3, "azimuth": 190.0, "rising": False})
        for cid, alias, trig, act in [
            (
                "1700000000001",
                "Hallway motion light",
                [{"platform": "state", "entity_id": "binary_sensor.hallway_motion", "to": "on"}],
                [{"service": "light.turn_on", "target": {"entity_id": "light.hallway"}}],
            ),
            (
                "1700000000002",
                "Evening blinds",
                [{"platform": "time", "at": "20:00:00"}],
                [{"service": "cover.close_cover", "target": {"entity_id": "cover.living_room_blinds"}}],
            ),
        ]:
            self.automations[cid] = {
                "id": cid,
                "alias": alias,
                "description": "",
                "triggers": trig,
                "conditions": [],
                "actions": act,
                "mode": "single",
            }
            self.put_automation(cid, self.automations[cid])
        self.history.clear()


# ---- HTTP ---------------------------------------------------------------------------------
async def _json(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        return {}


def create_app(state: HAState | None = None) -> FastAPI:
    ha = state or HAState.default()

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        ha.loop = asyncio.get_running_loop()
        yield
        ha.loop = None

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ha = ha

    @app.exception_handler(HAError)
    async def _err(_r, exc: HAError):
        return JSONResponse({"message": exc.message}, status_code=exc.status)

    async def guard(request: Request) -> None:
        if request.headers.get("authorization") != f"Bearer {ha.token}":
            raise HAError(401, "Unauthorized")
        if ha._fail > 0:
            ha._fail -= 1
            raise HAError(ha._fail_status, "Simulated failure")

    r = APIRouter(dependencies=[Depends(guard)])

    @r.get("/")
    async def root():
        return {"message": "API running."}

    @r.get("/config")
    async def config():
        return ha.config()

    @r.get("/states")
    async def states():
        return list(ha.states.values())

    @r.get("/states/{entity_id}")
    async def get_state(entity_id: str):
        if entity_id not in ha.states:
            raise HAError(404, "Entity not found.")
        return ha.states[entity_id]

    @r.post("/states/{entity_id}")
    async def post_state(entity_id: str, request: Request):
        body = await _json(request)
        new = entity_id not in ha.states
        s = ha.set_state(entity_id, body.get("state", "unknown"), body.get("attributes", {}))
        return JSONResponse(s, status_code=201 if new else 200)

    @r.post("/services/{domain}/{service}")
    async def service(domain: str, service: str, request: Request):
        body = await _json(request)
        return ha.call_service(domain, service, body if isinstance(body, dict) else {})

    async def history(request: Request, timestamp: str | None = None):
        q = request.query_params
        start = _dt(timestamp) if timestamp else _dt() - timedelta(days=1)
        end = _dt(q["end_time"]) if q.get("end_time") else _dt() + timedelta(seconds=1)
        ids = q.get("filter_entity_id")
        ids = ids.split(",") if ids else list(ha.states)
        return ha.history_for(ids, start, end)

    r.get("/history/period")(history)
    r.get("/history/period/{timestamp}")(history)

    @r.get("/config/automation/config/{cid}")
    async def get_auto(cid: str):
        if cid not in ha.automations:
            raise HAError(404, "Resource not found")
        return ha.automations[cid]

    @r.post("/config/automation/config/{cid}")
    async def post_auto(cid: str, request: Request):
        cfg = await _json(request)
        ha.validate_automation(cfg)
        ha.put_automation(cid, cfg)
        ha.config_writes.append(
            {"method": "POST", "id": cid, "config": ha.automations[cid], "ts": time.time()}
        )
        return {"result": "ok"}

    @r.delete("/config/automation/config/{cid}")
    async def del_auto(cid: str):
        if cid not in ha.automations:
            raise HAError(404, "Resource not found")
        ha.delete_automation(cid)
        ha.config_writes.append({"method": "DELETE", "id": cid, "config": None, "ts": time.time()})
        return {"result": "ok"}

    @r.post("/config/core/check_config")
    async def check_config():
        return {"result": "valid", "errors": None, "warnings": None}

    app.include_router(r, prefix="/api")
    app.include_router(r, prefix="/core/api")

    async def websocket(ws: WebSocket):
        await ws.accept()
        await ws.send_json({"type": "auth_required", "ha_version": ha.version})
        try:
            msg = await ws.receive_json()
            if msg.get("type") != "auth" or msg.get("access_token") != ha.token:
                await ws.send_json({"type": "auth_invalid", "message": "Invalid access token or password"})
                await ws.close()
                return
            await ws.send_json({"type": "auth_ok", "ha_version": ha.version})
            conn = _Conn(ws)
            ha._conns.add(conn)
            writer = asyncio.create_task(_writer(conn))
            try:
                while True:
                    await _handle(ha, conn, await ws.receive_json())
            finally:
                ha._conns.discard(conn)
                writer.cancel()
        except Exception:
            return

    app.add_api_websocket_route("/api/websocket", websocket)
    app.add_api_websocket_route("/core/websocket", websocket)
    return app


async def _writer(conn: _Conn) -> None:
    try:
        while True:
            await conn.ws.send_json(await conn.q.get())
    except Exception:
        return


async def _handle(ha: HAState, conn: _Conn, msg: dict) -> None:
    mid, typ = msg.get("id"), msg.get("type")

    def ok(result: Any = None) -> None:
        conn.q.put_nowait({"id": mid, "type": "result", "success": True, "result": result})

    def fail(code: str, message: str) -> None:
        conn.q.put_nowait(
            {"id": mid, "type": "result", "success": False, "error": {"code": code, "message": message}}
        )

    if typ == "ping":
        conn.q.put_nowait({"id": mid, "type": "pong"})
    elif typ == "subscribe_events":
        conn.subs[mid] = msg.get("event_type")
        ok()
    elif typ == "unsubscribe_events":
        if conn.subs.pop(msg.get("subscription"), 0) == 0:
            fail("not_found", "Subscription not found.")
        else:
            ok()
    elif typ == "get_states":
        ok(list(ha.states.values()))
    elif typ == "get_config":
        ok(ha.config())
    elif typ == "config/area_registry/list":
        ok(ha.areas)
    elif typ == "config/entity_registry/list":
        ok(ha.entity_registry)
    elif typ == "config/device_registry/list":
        ok(ha.devices)
    elif typ == "call_service":
        data = dict(msg.get("service_data") or {})
        tgt = (msg.get("target") or {}).get("entity_id")
        if tgt and "entity_id" not in data:
            data["entity_id"] = tgt
        try:
            changed = ha.call_service(msg.get("domain", ""), msg.get("service", ""), data)
            ok({"context": changed[0]["context"] if changed else _ctx(), "response": None})
        except HAError:
            fail("service_not_found", f"Service {msg.get('domain')}.{msg.get('service')} not found.")
    elif typ == "validate_config":
        singular = [k for k in ("trigger", "condition", "action") if k in msg]
        if singular:  # real HA wants the plural keys (docs/RESEARCH.md §3)
            fail("invalid_format", f"extra keys not allowed @ data['{singular[0]}']")
        else:
            ok({k: ha.validate_part(k, msg[k]) for k in ("triggers", "conditions", "actions") if k in msg})
    else:
        fail("unknown_command", "Unknown command.")
