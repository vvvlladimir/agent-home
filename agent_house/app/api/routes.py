"""JSON API for the GUI. Contract: contracts/openapi.yaml (paths there are relative to the ingress root)."""

from __future__ import annotations

import asyncio
import io
import json
import platform
import time
import zipfile
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from .. import __version__
from ..core.policy import READ_ONLY_DOMAINS, resolve_tier
from ..logbuf import ring

router = APIRouter(prefix="/api")


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts else None


def build_status(ctx) -> dict:
    kill = ctx.store.get("kill_switch", {"engaged": False, "since": None, "reason": None})
    settings = ctx.settings
    has_key = bool(settings.anthropic_api_key)
    local_ok = ctx.monitor.llm_state == "ok"
    provider = settings.orchestrator_provider
    if provider == "anthropic":
        effective = "anthropic" if has_key else "none"
    elif provider == "local":
        effective = "local" if local_ok else "none"
    else:
        effective = "anthropic" if has_key else ("local" if local_ok else "none")
    return {
        "version": __version__,
        "time": datetime.now(UTC).isoformat(),
        "mode": ctx.store.get("mode", "shadow"),
        "kill_switch": kill,
        "ha": ctx.ha.status(),
        "llm": ctx.monitor.llm_status(),
        "jetson": ctx.monitor.jetson_status(),
        "orchestrator": {
            "provider": provider,
            "effective": effective,
            "model": settings.anthropic_model if effective == "anthropic" else ctx.monitor.llm_model,
            "budget": {
                "daily_limit_eur": settings.daily_budget_eur,
                "spent_today_eur": 0.0,
                "tokens_in_today": 0,
                "tokens_out_today": 0,
                "exhausted": False,
            },
        },
        "counts": {
            "agents": 0,
            "pending_approvals": 0,
            "draft_proposals": 0,
            "deployed_automations": 0,
            "runs_today": 0,
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
    if ctx.store.get("mode", "shadow") != body.mode:
        ctx.store.set("mode", body.mode)
        ctx.feed.add(
            "system", f"Global mode set to {body.mode}", level="warning" if body.mode == "live" else "info"
        )
    return build_status(ctx)


class KillBody(BaseModel):
    engaged: bool
    reason: str | None = None


@router.put("/kill-switch")
async def set_kill_switch(body: KillBody, request: Request) -> dict:
    ctx = request.app.state.ctx
    current = ctx.store.get("kill_switch", {"engaged": False})
    if current.get("engaged") != body.engaged:
        ctx.store.set(
            "kill_switch",
            {
                "engaged": body.engaged,
                "since": datetime.now(UTC).isoformat() if body.engaged else None,
                "reason": (body.reason or "set in the GUI") if body.engaged else None,
            },
        )
        ctx.feed.add(
            "system",
            "Kill switch ENGAGED" if body.engaged else "Kill switch released",
            level="warning",
            data={"reason": body.reason},
        )
    return build_status(ctx)


@router.get("/areas")
async def list_areas(request: Request) -> list[dict]:
    reg = request.app.state.ctx.ha.registry
    states = request.app.state.ctx.ha.states
    out = []
    for area_id, area in sorted(reg.areas.items(), key=lambda kv: (kv[1].get("name") or kv[0]).lower()):
        count = sum(1 for e in reg.entities_in(area_id) if e in states and not reg.is_hidden(e))
        out.append(
            {"area_id": area_id, "name": area.get("name") or area_id, "entity_count": count, "agent_id": None}
        )
    return out


@router.get("/entities")
async def list_entities(request: Request, area: str | None = None, domain: str | None = None) -> list[dict]:
    ha = request.app.state.ctx.ha
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
                "tier": resolve_tier(entity_id),
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


@router.get("/settings")
async def get_settings(request: Request) -> dict:
    ctx = request.app.state.ctx
    masked = ctx.settings.masked()
    return {
        "options": masked,
        "runtime": {},
        "paths": {"data_dir": masked["data_dir"], "database": ctx.store.path},
    }


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
            "websocket_url": ctx.settings.ha_websocket_url,
        },
        "feed.json": ctx.feed.recent(limit=300),
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


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def not_implemented(path: str) -> JSONResponse:
    """Everything else in contracts/openapi.yaml arrives in a later milestone (see x-milestone)."""
    return JSONResponse(
        {"error": "not_implemented", "detail": f"api/{path} is not available in this build"}, status_code=501
    )
