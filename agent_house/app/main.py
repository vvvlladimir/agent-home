"""Application factory.  `uvicorn app.main:create_app --factory`"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api.agents import router as agents_router
from .api.routes import router
from .config import RuntimeSettings, Settings, load_runtime, load_settings
from .core.actuator import Actuator
from .core.agents import AgentStore
from .core.bench import BenchRunner
from .core.gate import EventGate
from .core.llm import LLMManager
from .core.monitor import JetsonMonitor
from .core.runtime import AgentRuntime, WakeRefused
from .core.safety import KILL_EVENT, Safety
from .ha.client import HAClient
from .logbuf import setup_logging
from .store.db import Store
from .store.feed import Feed

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "web" / "static"


@dataclass
class Context:
    settings: Settings
    store: Store
    feed: Feed
    ha: HAClient
    monitor: JetsonMonitor
    runtime: RuntimeSettings
    llm: LLMManager
    bench: BenchRunner
    safety: Safety
    agents: AgentStore
    agents_rt: AgentRuntime
    actuator: Actuator
    gate: EventGate
    ingress_stats: dict = field(
        default_factory=lambda: {"last_ingress_path": None, "last_peer": None, "rejected_peers": 0}
    )


class PeerGuard:
    """Only the Supervisor ingress proxy may connect (docs/RESEARCH.md §2).

    Pure ASGI middleware so it also covers streaming responses and WebSockets.
    """

    def __init__(self, app, ctx: Context) -> None:
        self.app, self.ctx = app, ctx
        self.allowed = set(ctx.settings.allowed_peers)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        peer = (scope.get("client") or ("?", 0))[0]
        if scope["path"] == "/healthz" and peer in ("127.0.0.1", "::1"):
            return await self.app(scope, receive, send)  # the container's own HEALTHCHECK
        if "*" not in self.allowed and peer not in self.allowed:
            self.ctx.ingress_stats["rejected_peers"] += 1
            log.warning("rejected connection from %s (allowed: %s)", peer, ", ".join(sorted(self.allowed)))
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 1008})
            response = JSONResponse(
                {"error": "forbidden", "detail": "only the ingress proxy may connect"}, 403
            )
            return await response(scope, receive, send)
        self.ctx.ingress_stats["last_peer"] = peer
        for name, value in scope.get("headers", []):
            if name == b"x-ingress-path":
                # /api/hassio_ingress/<token>: the token is a session secret, keep only the shape
                path = value.decode("latin-1").rstrip("/")
                head, _, token = path.rpartition("/")
                self.ctx.ingress_stats["last_ingress_path"] = f"{head}/<{len(token)} chars>" if head else path
                break
        return await self.app(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    setup_logging(settings.log_level)
    store = Store(settings.data_dir / "agent_house.db")
    feed = Feed(store)
    monitor = JetsonMonitor(settings)
    runtime = load_runtime(settings.data_dir)
    llm = LLMManager(settings, runtime, monitor)
    ha = HAClient(settings)
    safety = Safety(store, feed, settings.data_dir)
    agents = AgentStore(settings.data_dir)
    agents_rt = AgentRuntime(
        agents=agents, ha=ha, llm=llm, store=store, feed=feed, safety=safety, runtime_settings=runtime
    )

    def kill_entity() -> set[str]:
        return {ctx.runtime.kill_switch_entity} if ctx.runtime.kill_switch_entity else set()

    actuator = Actuator(store, feed, ha, safety, agents, agents_rt.scope, protected=kill_entity)

    def wake(agent_id: str, **kwargs) -> None:
        try:
            agents_rt.wake(agent_id, **kwargs)
        except WakeRefused as err:
            feed.add("gate_drop", f"{agent_id}: not woken, {err}", level="debug", agent_id=agent_id)

    gate = EventGate(agents, feed, store, agents_rt.scope, wake)
    agents_rt.actuator, agents_rt.gate = actuator, gate
    ctx = Context(
        settings=settings,
        store=store,
        feed=feed,
        ha=ha,
        monitor=monitor,
        runtime=runtime,
        llm=llm,
        bench=BenchRunner(llm, monitor, store, feed),
        safety=safety,
        agents=agents,
        agents_rt=agents_rt,
        actuator=actuator,
        gate=gate,
    )

    def on_state(entity_id: str, old: dict | None, new: dict | None) -> None:
        old_state = (old or {}).get("state")
        new_state = (new or {}).get("state")
        if old_state == new_state:
            return  # attribute-only updates are noise for the feed
        ctx.feed.add("state", f"{entity_id}: {old_state} → {new_state}", entity_id=entity_id, level="debug")
        # Optional kill switch helper in HA (Settings → kill_switch_entity): on = engaged.
        if entity_id == ctx.runtime.kill_switch_entity and new_state in ("on", "off"):
            safety.set_kill(new_state == "on", f"{entity_id} is {new_state}", source="Home Assistant")

    def on_kill_event(event: dict) -> None:
        """HA event `agent_house_kill_switch`; data: {engaged: bool = true, reason: str}."""
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        engaged = data.get("engaged") is not False  # anything unclear engages
        safety.set_kill(engaged, str(data.get("reason") or "")[:200] or None, source="Home Assistant event")

    ctx.ha.on_state_changed(on_state)
    ctx.ha.on_state_changed(gate.on_state)
    ctx.ha.on_event(KILL_EVENT, on_kill_event)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        log.info("Agent House %s starting (data dir %s)", __version__, settings.data_dir)
        if not settings.ha_token:
            log.error("no Home Assistant token: SUPERVISOR_TOKEN is missing (is homeassistant_api enabled?)")
        ctx.feed.add("system", f"Agent House {__version__} started")
        await ctx.ha.start()
        await ctx.monitor.start()
        await ctx.llm.start()
        await ctx.gate.start()

        async def sync_kill_helper() -> None:
            """The helper may have been switched on while the add-on was down."""
            entity = ctx.runtime.kill_switch_entity
            if entity and await ctx.ha.wait_ready(120) and ctx.ha.states.get(entity, {}).get("state") == "on":
                safety.set_kill(True, f"{entity} is on", source="Home Assistant")

        helper_sync = asyncio.create_task(sync_kill_helper(), name="kill-helper-sync")
        try:
            yield
        finally:
            helper_sync.cancel()
            await ctx.gate.stop()
            await ctx.agents_rt.stop()
            await ctx.bench.stop()
            await ctx.llm.stop()
            await ctx.monitor.stop()
            await ctx.ha.stop()
            store.close()

    app = FastAPI(title="Agent House", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.ctx = ctx
    app.include_router(agents_router)  # before `router`: its catch-all answers 501 for the rest
    app.include_router(router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"ok": True, "version": __version__}

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.add_middleware(PeerGuard, ctx=ctx)
    return app
