"""M5 API: chat with the orchestrator, and config export / import.
Contract: contracts/openapi.yaml (tags `chat`, `settings`)."""

from __future__ import annotations

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from ..config import REMOVED_RUNTIME, RuntimeSettings, save_runtime
from ..core.actuator import new_id
from ..core.orchestrator import OrchestratorError
from ..core.safety import ConfigError, check_whitelist, parse_policy

router = APIRouter(prefix="/api")
HTTP = {
    "not_found": 404, "kill_switch": 409, "disabled": 409, "unavailable": 503, "budget": 503,
    "failed": 422, "declined": 422,
}  # fmt: skip
SESSION_TURNS = 16  # stored messages per session (8 questions + 8 answers)
TURN_CHARS = 600
EXPORT_VERSION = 1
LEGACY_KEYS = ("agents", "active_profile")  # exports before 0.5.0 (D40): room agents, the llama profile


def _error(code: str, detail: str, status: int | None = None) -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status_code=status or HTTP.get(code, 409))


# --- chat -------------------------------------------------------------------------------------


@router.post("/chat")
async def chat(request: Request):
    """One turn with the orchestrator: it reads and drafts; it never actuates. `target` may be
    left out; anything but `orchestrator` is refused (D40: there are no room agents)."""
    ctx = request.app.state.ctx
    try:
        body = await request.json()
    except ValueError:
        body = None
    body = body if isinstance(body, dict) else {}
    target, message = body.get("target", "orchestrator"), body.get("message")
    if not isinstance(message, str) or not message.strip():
        return _error("bad_request", "a non-empty `message` is required", 400)
    if target != "orchestrator":
        return _error("not_found", f"no chat target {target!r}: only the orchestrator answers", 404)
    if len(message) > 4000:
        return _error("bad_request", "the message is longer than 4000 characters", 400)
    session_id = body.get("session_id") if isinstance(body.get("session_id"), str) else None
    key = f"chat:{session_id}" if session_id else None
    session = ctx.store.get(key) if key else None
    if not isinstance(session, dict) or session.get("target") != target:
        session_id, session = new_id("c"), {"target": target, "turns": []}
    history = session["turns"]
    try:
        reply, run_id = await ctx.orchestrator.chat(message, history)
    except OrchestratorError as err:
        return _error(err.code, str(err))
    history += [
        {"role": "user", "text": message.strip()[:TURN_CHARS]},
        {"role": "assistant", "text": str(reply)[:TURN_CHARS]},
    ]
    ctx.store.set(f"chat:{session_id}", {"target": target, "turns": history[-SESSION_TURNS:]})
    return {"session_id": session_id, "reply": reply, "run": ctx.runs.get(run_id)}


# --- config export / import ---------------------------------------------------------------------


@router.get("/config/export")
async def export_config(request: Request) -> Response:
    """Policy and GUI settings as one YAML file. No add-on options, so no secrets."""
    ctx = request.app.state.ctx
    document = {
        "agent_house_export": EXPORT_VERSION,
        "policy": ctx.safety.policy,
        "settings": ctx.runtime.model_dump(mode="json"),
    }
    return Response(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100),
        media_type="text/yaml",
        headers={"Content-Disposition": 'attachment; filename="agent-house-config.yaml"'},
    )


@router.post("/config/import")
async def import_config(request: Request):
    """All or nothing: everything is validated before anything is written. An export from before
    0.5.0 may carry `agents` and `active_profile`: they are ignored (D40) and named in `ignored`."""
    ctx = request.app.state.ctx
    try:
        document = yaml.safe_load((await request.body()).decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as err:
        return _error("invalid", f"not YAML: {str(err).splitlines()[0]}"[:300], 422)
    if not isinstance(document, dict) or document.get("agent_house_export") != EXPORT_VERSION:
        return _error("invalid", f"not an Agent House export (agent_house_export: {EXPORT_VERSION})", 422)
    unknown = sorted(set(document) - {"agent_house_export", "policy", "settings", *LEGACY_KEYS})
    if unknown:
        return _error("invalid", f"unknown key {unknown[0]!r}", 422)
    try:
        if document.get("policy") is not None:
            check_whitelist(document["policy"], ctx.safety.t3_whitelist)
        policy = parse_policy(document["policy"]) if document.get("policy") is not None else None
        settings = document.get("settings")
        if isinstance(settings, dict):
            settings = {k: v for k, v in settings.items() if k not in REMOVED_RUNTIME}
        runtime = RuntimeSettings(**settings) if settings is not None else None
    except ConfigError as err:
        return _error("invalid", str(err), 422)
    except (ValidationError, TypeError) as err:
        first = err.errors()[0] if isinstance(err, ValidationError) else {"loc": (), "msg": str(err)}
        where = ".".join(str(p) for p in first["loc"])
        return _error("invalid", f"settings.{where}: {first['msg']}", 422)
    if policy is not None:
        ctx.safety.save_policy(policy)
    if runtime is not None and runtime != ctx.runtime:
        save_runtime(ctx.settings.data_dir, runtime)
        ctx.runtime = ctx.learner.runtime = runtime
        ctx.council.settings = ctx.insights.settings = ctx.review.settings = ctx.manager.settings = runtime
    ignored = [k for k in LEGACY_KEYS if k in document]
    ctx.feed.add(
        "system",
        "Config imported: "
        + (", ".join(k for k, v in (("policy", policy), ("settings", runtime)) if v is not None) or "nothing")
        + (f" (ignored: {', '.join(ignored)})" if ignored else ""),
        level="warning",
    )
    return {"policy": policy is not None, "settings": runtime is not None, "ignored": ignored}
