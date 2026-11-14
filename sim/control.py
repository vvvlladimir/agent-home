"""The sim panel: a control API and a no-build static page on 127.0.0.1:8090.

Everything that touches the fake house runs on the fake HA's event loop (`on_ha`), so the panel,
the house's life and the add-on never race. Mutating requests need the `X-Sim-Panel` header
(a cross-site form cannot set it) and a localhost `Host` (no DNS rebinding).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from sim import house_gen
from sim.fake_ha import HAState
from sim.fake_jetson import JetsonSim
from sim.fake_llm import LLMSim
from sim.gateways import Gateways
from sim.life import Life
from sim.scenarios import SCENARIOS
from sim.sim_config import SECRET_KEYS, SimConfig

PANEL_DIR = Path(__file__).resolve().parent / "panel"
JETSON_FIELDS = {
    "temp_tj_c": (20, 110),
    "temp_cpu_c": (20, 110),
    "temp_gpu_c": (20, 110),
    "ram_used_mb": (500, 7620),
    "swap_used_mb": (0, 3810),
    "cpu_percent": (0, 100),
    "gpu_percent": (0, 100),
    "power_mw": (2000, 25000),
}
CAUSES = ("user", "physical", "automation")


class Sim:
    """Everything the panel steers, wired by sim/run.py."""

    def __init__(
        self,
        ha: HAState,
        llm: LLMSim,
        jetson: JetsonSim,
        cfg: SimConfig,
        gateways: Gateways,
        app_url: str = "",
    ) -> None:
        self.ha, self.llm, self.jetson, self.cfg, self.gw = ha, llm, jetson, cfg, gateways
        self.app_url = app_url
        self.life: Life | None = None
        self.built: house_gen.Built | None = None
        self.backfilled: dict = {}

    async def on_ha(self, fn: Callable[[], Any]) -> Any:
        """Run `fn` on the fake HA's loop (where the house lives) and wait for its result."""
        loop = self.ha.loop
        if loop is None:
            return fn()

        async def call():
            return fn()

        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(call(), loop))

    # ---- house -----------------------------------------------------------------------------------
    def spec(self) -> house_gen.HouseSpec:
        data = dict(self.cfg.house or {})
        preset = data.pop("preset", "realistic")
        known = {k: v for k, v in data.items() if k in house_gen.HouseSpec.__dataclass_fields__}
        return house_gen.HouseSpec.from_preset(
            preset if preset in house_gen.PRESETS else "realistic", **known
        )

    def rebuild(self, spec: house_gen.HouseSpec, backfill_days: int) -> dict:
        """On the HA loop: grow the house, restart its streams, write history."""
        assert self.life is not None
        self.built, streams = house_gen.build(self.ha, spec)
        self.life.set_streams(streams)
        self.backfilled = self.life.backfill(backfill_days, owner_tracker="device_tracker.iphone")
        return self.state_house()

    def state_house(self) -> dict:
        built = self.built
        return {
            "spec": self.spec().to_dict(),
            "summary": house_gen.summary(self.ha),
            "built": None
            if built is None
            else {
                "devices": built.devices,
                "entities": built.entities,
                "registry_only": built.registry_only,
                "junk": built.junk,
            },
            "backfill": self.backfilled,
        }

    def entities(self, q: str, domain: str, junk: bool, limit: int) -> list[dict]:
        reg = {e["entity_id"]: e for e in self.ha.entity_registry}
        dev_area = {d["id"]: d.get("area_id") for d in self.ha.devices}
        out = []
        for eid, s in sorted(self.ha.states.items()):
            if domain and not eid.startswith(f"{domain}."):
                continue
            name = str(s["attributes"].get("friendly_name") or "")
            if q and q.lower() not in eid and q.lower() not in name.lower():
                continue
            r = reg.get(eid, {})
            bad = s["state"] in ("unavailable", "unknown") or bool(s["attributes"].get("restored"))
            if junk and not bad:
                continue
            out.append(
                {
                    "entity_id": eid,
                    "state": s["state"],
                    "name": name,
                    "last_changed": s["last_changed"],
                    "area": r.get("area_id") or dev_area.get(r.get("device_id")),
                    "platform": r.get("platform"),
                    "category": r.get("entity_category"),
                    "generated": eid in self.ha.generated,
                    "junk": bad,
                }
            )
            if len(out) >= limit:
                break
        return out

    def set_entity(self, eid: str, state: str, cause: str, attributes: dict | None) -> dict:
        if eid not in self.ha.states:
            raise KeyError(eid)
        if attributes:
            self.ha.states[eid]["attributes"].update(attributes)
        if cause == "user":
            return self.ha.user_change(eid, state)
        if cause == "automation":
            self.ha.run_automation("automation.sim_panel", [(eid, state)])
            return self.ha.states[eid]
        return self.ha.physical_change(eid, state)

    # ---- faults ----------------------------------------------------------------------------------
    def ha_restart(self, seconds: float) -> None:
        """What the add-on sees while HA restarts: the WebSocket drops, REST answers 502 for a while."""
        self.ha.drop_ws()
        self.ha.fail_next(max(1, int(seconds * 2)), 502)

    def state(self) -> dict:
        assert self.life is not None
        return {
            "app_url": self.app_url,
            "house": self.state_house(),
            "presets": house_gen.PRESETS,
            "life": self.life.stats(),
            "scenarios": {k: v[0] for k, v in SCENARIOS.items()},
            "config": self.cfg.public(),
            "ha": {
                "latency_s": self.ha.latency,
                "fail_next": self.ha._fail,
                "fail_status": self.ha._fail_status,
                "ws_connections": len(self.ha._conns),
                "version": self.ha.version,
            },
            "fake_llm": {
                "loading": self.llm.loading,
                "latency_s": self.llm.latency,
                "queued_script": len(self.llm.script),
                "requests": len(self.llm.requests),
            },
            "jetson": {
                **{k: getattr(self.jetson, k) for k in JETSON_FIELDS},
                "throttled": self.jetson.throttled,
                "active_profile": self.jetson.active_profile,
                "profiles": self.jetson.profiles,
            },
        }


# ---- connection checks: cheap calls that cost no tokens ------------------------------------------
async def check(cfg: SimConfig, target: str) -> dict:
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            if target == "jetson":
                if not cfg.jetson.host:
                    return {"ok": False, "detail": "no Jetson address"}
                token = cfg.secret("jetson_token")
                h = {"Authorization": f"Bearer {token}"} if token else {}
                health = await c.get(f"{cfg.jetson.llm_url()}/health", headers=h)
                metrics = await c.get(f"{cfg.jetson.metrics_url()}/metrics.json", headers=h)
                m = metrics.json() if metrics.status_code == 200 else {}
                detail = (
                    f"llama-server /health {health.status_code}, metrics {metrics.status_code}, "
                    f"profile {(m.get('llama') or {}).get('active_profile')}"
                )
                ok = health.status_code == 200 and metrics.status_code == 200
            elif target == "anthropic":
                r = await c.get(
                    "https://api.anthropic.com/v1/models",
                    headers={"x-api-key": cfg.secret("anthropic_api_key"), "anthropic-version": "2023-06-01"},
                )
                ok, detail = r.status_code == 200, f"GET /v1/models → {r.status_code}"
            elif target in ("openai", "compatible"):
                base = (
                    "https://api.openai.com/v1"
                    if target == "openai"
                    else cfg.room.compatible_base_url.rstrip("/")
                )
                key = cfg.secret(f"{target}_api_key")
                r = await c.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"})
                ok, detail = r.status_code == 200, f"GET {base}/models → {r.status_code}"
                if ok and target == "compatible":
                    ids = [m.get("id") for m in (r.json().get("data") or [])]
                    found = cfg.room.compatible_model in ids
                    detail += f"; model {cfg.room.compatible_model!r} {'listed' if found else 'NOT listed'}"
                    ok = found or not ids
            else:
                return {"ok": False, "detail": f"unknown target {target}"}
    except (httpx.HTTPError, ValueError) as err:
        return {"ok": False, "detail": f"{type(err).__name__}: {err}"[:200]}
    return {"ok": ok, "detail": detail, "ms": round((time.monotonic() - started) * 1000)}


def create_app(sim: Sim, port: int) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host") not in hosts:
            return JSONResponse({"detail": "sim panel: localhost only"}, status_code=403)
        if request.method not in ("GET", "HEAD") and request.headers.get("x-sim-panel") != "1":
            return JSONResponse({"detail": "sim panel: missing X-Sim-Panel header"}, status_code=403)
        return await call_next(request)

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(400, "body is not JSON") from None
        if not isinstance(data, dict):
            raise HTTPException(400, "body must be an object")
        return data

    @app.get("/")
    async def index():
        return FileResponse(PANEL_DIR / "index.html")

    @app.get("/api/state")
    async def state():
        return await sim.on_ha(sim.state)

    # -- house
    @app.post("/api/house")
    async def house(request: Request):
        data = await body(request)
        days = data.pop("backfill_days", sim.cfg.backfill_days)
        try:
            preset = data.pop("preset", "realistic")
            spec = house_gen.HouseSpec.from_preset(
                preset, **{k: v for k, v in data.items() if k in house_gen.HouseSpec.__dataclass_fields__}
            )
            spec.clamp()
            sim.cfg.update({"house": spec.to_dict(), "backfill_days": days})
        except (ValueError, TypeError) as err:
            raise HTTPException(400, str(err)) from None
        sim.cfg.save()
        return await sim.on_ha(lambda: sim.rebuild(spec, sim.cfg.backfill_days))

    @app.post("/api/backfill")
    async def backfill(request: Request):
        days = int((await body(request)).get("days", 14))
        assert sim.life
        result = await sim.on_ha(lambda: sim.life.backfill(days, owner_tracker="device_tracker.iphone"))
        sim.backfilled = result
        return result

    # -- flow
    @app.post("/api/flow")
    async def flow(request: Request):
        data = await body(request)
        life = sim.life
        assert life

        def apply():
            if "rate" in data:
                life.rate = max(0.0, min(float(data["rate"]), 50.0))
            if "speed" in data:
                life.speed = max(0.1, min(float(data["speed"]), 100.0))
            if "run_automations" in data:
                life.run_automations = bool(data["run_automations"])
            if storm := data.get("storm"):
                life.storm(float(storm.get("seconds", 30)), float(storm.get("factor", 20)))
            return life.stats()

        return await sim.on_ha(apply)

    @app.get("/api/events")
    async def events(limit: int = 100):
        assert sim.life
        return await sim.on_ha(lambda: list(reversed(sim.life.events))[: max(1, min(limit, 300))])

    # -- entities
    @app.get("/api/entities")
    async def entities(q: str = "", domain: str = "", junk: bool = False, limit: int = 200):
        return await sim.on_ha(lambda: sim.entities(q, domain, junk, max(1, min(limit, 2000))))

    @app.post("/api/entities/{entity_id}")
    async def set_entity(entity_id: str, request: Request):
        data = await body(request)
        cause = data.get("cause", "physical")
        if cause not in CAUSES or not isinstance(data.get("state"), str):
            raise HTTPException(400, f"need a string state and a cause in {CAUSES}")
        try:
            return await sim.on_ha(
                lambda: sim.set_entity(entity_id, data["state"], cause, data.get("attributes"))
            )
        except KeyError:
            raise HTTPException(404, f"no entity {entity_id}") from None

    @app.delete("/api/entities/{entity_id}")
    async def remove_entity(entity_id: str):
        if entity_id not in sim.ha.states:
            raise HTTPException(404, f"no entity {entity_id}")
        await sim.on_ha(lambda: sim.ha.remove_state(entity_id))
        return {"removed": entity_id}

    # -- scenarios
    @app.post("/api/scenario/{name}")
    async def scenario(name: str):
        if name not in SCENARIOS:
            raise HTTPException(404, f"unknown scenario {name}")
        life = sim.life
        assert life

        def start():
            steps = SCENARIOS[name][1](sim.ha)
            life.play(name, steps)
            return {"scenario": name, "steps": len(steps)}

        return await sim.on_ha(start)

    @app.post("/api/scenario-stop")
    async def scenario_stop():
        life = sim.life
        assert life

        def stop():
            if life._scenario:
                life._scenario.cancel()
            life.scenario_name = None
            return {"stopped": True}

        return await sim.on_ha(stop)

    # -- faults
    @app.post("/api/faults")
    async def faults(request: Request):
        data = await body(request)
        target = data.get("target")

        def apply():
            if target == "ha":
                if "latency_s" in data:
                    sim.ha.latency = max(0.0, min(float(data["latency_s"]), 60.0))
                if "fail_next" in data:
                    sim.ha.fail_next(max(0, int(data["fail_next"])), int(data.get("status", 500)))
                if data.get("ws_drop"):
                    sim.ha.drop_ws()
                if data.get("restart"):
                    sim.ha_restart(float(data.get("seconds", 10)))
            elif target in ("llm", "anthropic", "openai"):
                f = sim.cfg.faults[target]
                if "fail_next" in data:
                    f.fail_next, f.status = max(0, int(data["fail_next"])), int(data.get("status", 500))
                if "latency_s" in data:
                    f.latency_s = max(0.0, min(float(data["latency_s"]), 300.0))
            elif target == "fake_llm":
                if "loading" in data:
                    sim.llm.loading = bool(data["loading"])
                if "script" in data and isinstance(data["script"], list):
                    sim.llm.script.extend(s for s in data["script"] if isinstance(s, dict))
            elif target == "jetson":
                for k, (lo, hi) in JETSON_FIELDS.items():
                    if k in data:
                        setattr(sim.jetson, k, max(lo, min(float(data[k]), hi)))
                if "throttled" in data:
                    sim.jetson.throttled = bool(data["throttled"])
            else:
                raise ValueError(f"unknown target {target!r}")
            return sim.state()

        try:
            return await sim.on_ha(apply)
        except (ValueError, TypeError) as err:
            raise HTTPException(400, str(err)) from None

    # -- models and keys
    @app.put("/api/config")
    async def put_config(request: Request):
        try:
            sim.cfg.update(await body(request))
        except (ValueError, TypeError) as err:
            raise HTTPException(400, str(err)) from None
        sim.cfg.save()
        return sim.cfg.public()

    @app.put("/api/secrets")
    async def put_secrets(request: Request):
        data = await body(request)
        try:
            for k, v in data.items():
                if not isinstance(v, str):
                    raise ValueError(f"{k} must be a string")
                sim.cfg.set_secret(k, v)
        except ValueError as err:
            raise HTTPException(400, f"{err}; known: {', '.join(SECRET_KEYS)}") from None
        sim.cfg.save()
        return sim.cfg.public()["secrets"]

    @app.post("/api/check/{target}")
    async def check_target(target: str):
        return await check(sim.cfg, target)

    @app.get("/api/requests")
    async def requests_log():
        return sim.gw.log.list()

    app.mount("/static", StaticFiles(directory=PANEL_DIR), name="static")
    return app
