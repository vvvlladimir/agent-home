"""JSON API for the GUI. Contract: contracts/openapi.yaml (paths there are relative to the ingress root)."""

from __future__ import annotations

import asyncio
import difflib
import io
import json
import platform
import time
import zipfile
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, ValidationError

from .. import __version__
from ..config import RuntimeSettings, redact_url, save_runtime
from ..core import profiles
from ..core.bench import BenchBusy
from ..core.monitor import JetsonError
from ..core.policy import READ_ONLY_DOMAINS, resolve_tier
from ..logbuf import ring

router = APIRouter(prefix="/api")


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts else None


def build_status(ctx) -> dict:
    kill = ctx.safety.kill
    return {
        "version": __version__,
        "time": datetime.now(UTC).isoformat(),
        "mode": ctx.safety.mode,
        "kill_switch": kill,
        "ha": ctx.ha.status(),
        "llm": ctx.llm.status(),
        "jetson": ctx.monitor.jetson_status(),
        "orchestrator": ctx.orchestrator.status(),
        "counts": {
            "agents": len(ctx.agents.agents),
            "pending_approvals": ctx.actuator.pending_count(),
            "draft_proposals": ctx.lifecycle.count_open(),
            "deployed_automations": ctx.lifecycle.count_deployed(),
            "runs_today": ctx.agents_rt.runs_today(),
        },
    }


@router.get("/status")
async def get_status(request: Request) -> dict:
    return build_status(request.app.state.ctx)


class ModeBody(BaseModel):
    mode: str


@router.put("/mode")
async def set_mode(body: ModeBody, request: Request):
    ctx = request.app.state.ctx
    if body.mode not in ("shadow", "live"):
        return JSONResponse({"error": "invalid", "detail": "mode must be shadow or live"}, status_code=422)
    ctx.safety.set_mode(body.mode)
    return build_status(ctx)


class KillBody(BaseModel):
    engaged: bool
    reason: str | None = Field(default=None, max_length=200)


@router.put("/kill-switch")
async def set_kill_switch(body: KillBody, request: Request) -> dict:
    ctx = request.app.state.ctx
    ctx.safety.set_kill(body.engaged, body.reason)
    return build_status(ctx)


@router.get("/areas")
async def list_areas(request: Request) -> list[dict]:
    reg = request.app.state.ctx.ha.registry
    states = request.app.state.ctx.ha.states
    by_room: dict[str, str] = {}
    for agent in request.app.state.ctx.agents.list():
        by_room.setdefault(agent.room, agent.id)
    out = []
    for area_id, area in sorted(reg.areas.items(), key=lambda kv: (kv[1].get("name") or kv[0]).lower()):
        count = sum(1 for e in reg.entities_in(area_id) if e in states and not reg.is_hidden(e))
        out.append(
            {
                "area_id": area_id,
                "name": area.get("name") or area_id,
                "entity_count": count,
                "agent_id": by_room.get(area_id),
            }
        )
    return out


@router.get("/entities")
async def list_entities(request: Request, area: str | None = None, domain: str | None = None) -> list[dict]:
    ha = request.app.state.ctx.ha
    policy = request.app.state.ctx.safety.policy
    out = []
    for entity_id in sorted(ha.states):
        state = ha.states[entity_id]
        entity_domain = entity_id.split(".", 1)[0]
        entity_area = ha.registry.area_of(entity_id)
        if domain and entity_domain != domain:
            continue
        if area == "none":
            if entity_area is not None:
                continue
        elif area and entity_area != area:
            continue
        attrs = state.get("attributes") or {}
        out.append(
            {
                "entity_id": entity_id,
                "domain": entity_domain,
                "name": ha.registry.name_of(entity_id) or attrs.get("friendly_name"),
                "area_id": entity_area,
                "state": state.get("state"),
                "unit": attrs.get("unit_of_measurement"),
                "device_class": attrs.get("device_class"),
                "tier": resolve_tier(entity_id, policy),
                "read_only": entity_domain in READ_ONLY_DOMAINS,
                "last_changed": state.get("last_changed"),
                "hidden": ha.registry.is_hidden(entity_id),
            }
        )
    return out


@router.get("/events")
async def list_events(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    kind: str | None = None,
    before: int | None = None,
) -> list[dict]:
    return request.app.state.ctx.feed.recent(limit=limit, kind=kind, before=before)


@router.get("/events/stream")
async def stream_events(request: Request) -> StreamingResponse:
    feed = request.app.state.ctx.feed

    async def generate():
        queue = feed.subscribe()
        try:
            yield ": connected\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(item, separators=(',', ':'))}\n\n"
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            feed.unsubscribe(queue)

    # X-Accel-Buffering: the Supervisor ingress is an aiohttp proxy, but users also put nginx in front.
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(generate(), media_type="text/event-stream", headers=headers)


def _error(code: str, detail: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status_code=status)


def _settings_view(ctx) -> dict:
    masked = ctx.settings.masked()
    return {
        "options": masked,
        "runtime": ctx.runtime.model_dump(mode="json"),
        "paths": {
            "data_dir": masked["data_dir"],
            "database": ctx.store.path,
            "runtime_settings": str(ctx.settings.data_dir / "settings.yaml"),
        },
    }


@router.get("/settings")
async def get_settings(request: Request) -> dict:
    return _settings_view(request.app.state.ctx)


@router.put("/settings")
async def put_settings(request: Request):
    """Partial update of the GUI-editable settings; add-on options are not reachable from here."""
    ctx = request.app.state.ctx
    try:
        changes = await request.json()
    except ValueError:
        return _error("invalid", "the body is not JSON", 422)
    if not isinstance(changes, dict):
        return _error("invalid", "the body must be an object of settings", 422)
    options = sorted(
        k for k in changes if k not in RuntimeSettings.model_fields and k in ctx.settings.masked()
    )
    if options:
        return _error(
            "invalid",
            f"{', '.join(options)}: an add-on option, read-only here; change it in Home Assistant under "
            "Settings → Apps (Add-ons before HA 2026.2) → Agent House → Configuration (it restarts)",
            422,
        )
    try:
        updated = RuntimeSettings(**{**ctx.runtime.model_dump(), **changes})
    except ValidationError as err:
        first = err.errors()[0]
        return _error("invalid", f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}", 422)
    if updated != ctx.runtime:
        save_runtime(ctx.settings.data_dir, updated)
        ctx.runtime = ctx.llm.runtime = ctx.agents_rt.settings = updated
        ctx.council.settings = ctx.insights.settings = ctx.review.settings = ctx.manager.settings = updated
        ctx.feed.add("system", "Settings changed", data={"changed": sorted(changes)})
    return _settings_view(ctx)


# --- models & benchmarks (M2) -----------------------------------------------------------------


@router.get("/models/profiles")
async def list_profiles(request: Request) -> list[dict]:
    active = request.app.state.ctx.llm.active_profile
    return [{**profile, "active": profile["name"] == active} for profile in profiles.PROFILES]


class ProfileBody(BaseModel):
    profile: str = Field(max_length=64)


@router.put("/models/active")
async def set_active_profile(body: ProfileBody, request: Request) -> JSONResponse:
    ctx = request.app.state.ctx
    known = {p["name"] for p in profiles.PROFILES}
    if body.profile not in known:
        return _error("not_found", f"unknown profile; known: {', '.join(sorted(known))}", 404)
    if not ctx.monitor.can_switch:
        return _error(
            "not_configured", "set jetson_metrics_url and jetson_token in the add-on configuration", 409
        )
    if ctx.bench.running:
        return _error("busy", "a benchmark is running", 409)
    if ctx.llm.swapping:
        return _error("busy", "a model swap is already in progress", 409)
    ctx.llm.begin_swap(body.profile)
    try:
        await ctx.monitor.switch_profile(body.profile)
    except JetsonError as err:
        ctx.llm.end_swap()
        await ctx.llm.probe()
        ctx.feed.add("system", f"Model swap to {body.profile} failed: {err}", level="error")
        return _error("jetson_error", str(err), 502)
    ctx.feed.add("system", f"Model profile switching to {body.profile}", level="warning")
    await ctx.llm.probe()
    return JSONResponse(ctx.llm.status(), status_code=202)


@router.get("/benchmarks")
async def list_benchmarks(request: Request) -> list[dict]:
    return request.app.state.ctx.bench.list()


class BenchBody(BaseModel):
    profile: str | None = Field(default=None, max_length=64)
    quick: bool = False


@router.post("/benchmarks")
async def run_benchmark(request: Request, body: BenchBody | None = None) -> JSONResponse:
    ctx = request.app.state.ctx
    body = body or BenchBody()
    active = ctx.llm.active_profile
    if body.profile and active and body.profile != active:
        return _error("wrong_profile", f"{active} is loaded; activate {body.profile} first", 409)
    try:
        return JSONResponse({"id": ctx.bench.start(quick=body.quick)}, status_code=202)
    except BenchBusy as err:
        return _error("busy", str(err), 409)


@router.get("/logs")
async def get_logs(limit: int = Query(200, ge=1, le=2000), level: str = "debug") -> list[dict]:
    return ring.tail(limit=limit, level=level)


@router.get("/diagnostics")
async def get_diagnostics(request: Request) -> Response:
    ctx = request.app.state.ctx
    ha = ctx.ha
    rest_alive = await ha.api_alive()
    domains: dict[str, int] = {}
    for entity_id in ha.states:
        domains[entity_id.split(".", 1)[0]] = domains.get(entity_id.split(".", 1)[0], 0) + 1
    files = {
        "status.json": build_status(ctx),
        "settings.redacted.json": ctx.settings.masked(),
        "registry_summary.json": {
            "areas": {a: len(ha.registry.entities_in(a)) for a in ha.registry.areas},
            "entities_without_area": sum(1 for e in ha.states if ha.registry.area_of(e) is None),
            "domains": domains,
            "rest_api_alive": rest_alive,
            "websocket_url": redact_url(ctx.settings.ha_websocket_url),
        },
        "feed.json": ctx.feed.recent(limit=300),
        "runtime_settings.json": ctx.runtime.model_dump(mode="json"),
        "benchmarks.json": ctx.bench.list(limit=20),
        "agents.json": [agent.dump() for agent in ctx.agents.list()],
        "policy.json": ctx.safety.policy,
        "runs.json": ctx.agents_rt.list_runs(limit=50),
        "actions.json": ctx.actuator.list(limit=100),
        "proposals.json": ctx.lifecycle.list(),
        "automations.json": ctx.lifecycle.deployed(),
    }
    log_text = "\n".join(
        f"{_iso(r['ts'])} {r['level'].upper():7} {r['logger']}: {r['message']}" for r in ring.tail(limit=2000)
    )
    manifest = {
        "schema_version": 1,
        "kind": "diagnostics_bundle",
        "created": datetime.now(UTC).isoformat(),
        "app_version": __version__,
        "python": platform.python_version(),
        "arch": platform.machine(),
        "ha": {
            "connected": ha.connected,
            "version": ha.version,
            "via_supervisor": ctx.settings.via_supervisor,
            "areas": len(ha.registry.areas),
            "entities": len(ha.states),
        },
        "ingress": ctx.ingress_stats,
        "jetson": ctx.monitor.jetson_status(),
        "files": [*files, "log.txt"],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest, indent=2))
        for name, content in files.items():
            bundle.writestr(name, json.dumps(content, indent=2, default=str))
        bundle.writestr("log.txt", log_text)
    name = f"agent-house-diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _api_routes(routes) -> list[APIRoute]:
    """Every APIRoute, also inside included routers (newer FastAPI wraps them)."""
    out: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            out.append(route)
        elif isinstance(inner := getattr(route, "routes", None), list):
            out += _api_routes(inner)
        else:
            for attr in ("original_router", "router"):  # FastAPI ≥0.13x: _IncludedRouter
                sub = getattr(route, attr, None)
                if sub is not None and isinstance(getattr(sub, "routes", None), list):
                    out += _api_routes(sub.routes)
                    break
    return out


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def unknown_route(path: str, request: Request) -> JSONResponse:
    """Nothing else matched: 405 with `Allow` when the path exists for other methods (RFC 9110
    §15.5.6), else 404 that names the routes under or near that path."""
    target = f"/api/{path}".rstrip("/")
    allowed: set[str] = set()
    known: list[str] = []
    for route in _api_routes(request.app.routes):
        if route.endpoint is unknown_route:
            continue
        if route.path_regex.match(target):
            allowed |= route.methods or set()
        if route.path.startswith("/api/"):
            known += [f"{m} {route.path}" for m in sorted(route.methods or ())]
    if allowed:
        allow = ", ".join(sorted(allowed - {"HEAD"}))
        return JSONResponse(
            {
                "error": "method_not_allowed",
                "detail": f"{request.method} is not allowed on {target}; use {allow}",
            },
            status_code=405,
            headers={"Allow": allow},
        )
    under = [k for k in known if k.split(" ", 1)[1].startswith(target + "/")]
    near = under or [
        k for k in known
        if k.split(" ", 1)[1] in difflib.get_close_matches(target, [x.split(" ", 1)[1] for x in known], n=3)
    ]  # fmt: skip
    hint = f"; did you mean: {', '.join(near[:8])}" if near else ""
    return JSONResponse({"error": "not_found", "detail": f"no API route {target}{hint}"}, status_code=404)
