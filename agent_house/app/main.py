"""Application factory.  `uvicorn app.main:create_app --factory`"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api.routes import router
from .config import Settings, load_settings
from .core.monitor import JetsonMonitor
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
                self.ctx.ingress_stats["last_ingress_path"] = value.decode("latin-1")
                break
        return await self.app(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    setup_logging(settings.log_level)
    store = Store(settings.data_dir / "agent_house.db")
    ctx = Context(
        settings=settings,
        store=store,
        feed=Feed(store),
        ha=HAClient(settings),
        monitor=JetsonMonitor(settings),
    )

    def on_state(entity_id: str, old: dict | None, new: dict | None) -> None:
        old_state = (old or {}).get("state")
        new_state = (new or {}).get("state")
        if old_state == new_state:
            return  # attribute-only updates are noise for the feed
        ctx.feed.add("state", f"{entity_id}: {old_state} → {new_state}", entity_id=entity_id, level="debug")

    ctx.ha.on_state_changed(on_state)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        log.info("Agent House %s starting (data dir %s)", __version__, settings.data_dir)
        if not settings.ha_token:
            log.error("no Home Assistant token: SUPERVISOR_TOKEN is missing (is homeassistant_api enabled?)")
        ctx.feed.add("system", f"Agent House {__version__} started")
        await ctx.ha.start()
        await ctx.monitor.start()
        try:
            yield
        finally:
            await ctx.monitor.stop()
            await ctx.ha.stop()
            store.close()

    app = FastAPI(title="Agent House", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.ctx = ctx
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
