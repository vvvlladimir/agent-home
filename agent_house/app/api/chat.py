"""M5 API: chat with the orchestrator or a room agent, and config export / import.
Contract: contracts/openapi.yaml (tags `chat`, `settings`)."""

from __future__ import annotations

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from ..config import RuntimeSettings, save_runtime
from ..core.actuator import new_id
from ..core.agents import ConfigError, parse_agent, parse_policy
from ..core.orchestrator import OrchestratorError
from ..core.runtime import WakeRefused

router = APIRouter(prefix="/api")
HTTP = {
    "not_found": 404, "kill_switch": 409, "disabled": 409, "unavailable": 503, "budget": 503,
    "failed": 422, "declined": 422,
}  # fmt: skip
SESSION_TURNS = 16  # stored messages per session (8 questions + 8 answers)
TURN_CHARS = 600
EXPORT_VERSION = 1


def _error(code: str, detail: str, status: int | None = None) -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status_code=status or HTTP.get(code, 409))


# --- chat -------------------------------------------------------------------------------------


@router.post("/chat")
async def chat(request: Request):
    """One turn. A room agent answers through a normal run (wake source `chat`): same tools, same
    policy, same shadow mode. The orchestrator reads and drafts; it never actuates."""
    ctx = request.app.state.ctx
    try:
        body = await request.json()
    except ValueError:
        body = None
    body = body if isinstance(body, dict) else {}
    target, message = body.get("target"), body.get("message")
    if not isinstance(target, str) or not isinstance(message, str) or not message.strip():
        return _error("bad_request", "`target` and a non-empty `message` are required", 400)
    if len(message) > 4000:
        return _error("bad_request", "the message is longer than 4000 characters", 400)
    session_id = body.get("session_id") if isinstance(body.get("session_id"), str) else None
    key = f"chat:{session_id}" if session_id else None
    session = ctx.store.get(key) if key else None
    if not isinstance(session, dict) or session.get("target") != target:
        session_id, session = new_id("c"), {"target": target, "turns": []}
    history = session["turns"]
    try:
        if target == "orchestrator":
            reply, run_id = await ctx.orchestrator.chat(message, history)
            run = ctx.agents_rt.get_run(run_id)
        else:
            earlier = "".join(
                f"{'Owner' if h['role'] == 'user' else 'You'}: {h['text']}\n" for h in history[-8:]
            )
            prompt = (
                "The owner writes to you in chat. Answer the question from what you read; act only "
                "if the owner asks you to.\n"
                + (f"Earlier in this chat:\n{earlier}" if earlier else "")
                + f"Owner: {message.strip()}"
            )
            run_id = ctx.agents_rt.wake(
                target, source="chat", reason=f"Owner in chat: {message.strip()}", prompt_reason=prompt
            )
            run = await ctx.agents_rt.wait(run_id)
            final = next((s["data"].get("text") for s in run["trace"][::-1] if s["type"] == "final"), None)
            reply = final or run.get("summary") or run.get("error") or f"(run {run['status']})"
    except (OrchestratorError, WakeRefused) as err:
        return _error(err.code, str(err))
    history += [
        {"role": "user", "text": message.strip()[:TURN_CHARS]},
        {"role": "assistant", "text": str(reply)[:TURN_CHARS]},
    ]
    ctx.store.set(f"chat:{session_id}", {"target": target, "turns": history[-SESSION_TURNS:]})
    return {"session_id": session_id, "reply": reply, "run": run}


# --- config export / import ---------------------------------------------------------------------


@router.get("/config/export")
async def export_config(request: Request) -> Response:
    """Agents, policy and GUI settings as one YAML file. No add-on options, so no secrets."""
    ctx = request.app.state.ctx
    document = {
        "agent_house_export": EXPORT_VERSION,
        "agents": [agent.dump() for agent in ctx.agents.list()],
        "policy": ctx.safety.policy,
        "settings": ctx.runtime.model_dump(mode="json"),
        "active_profile": ctx.llm.active_profile,  # informational: the profile lives on the Jetson
    }
    return Response(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100),
        media_type="text/yaml",
        headers={"Content-Disposition": 'attachment; filename="agent-house-config.yaml"'},
    )


@router.post("/config/import")
async def import_config(request: Request):
    """All or nothing: everything is validated before anything is written. Imported agents always
    start in Shadow Mode (the owner enables live per agent, here, not through a file); agents that
    the file does not name are kept."""
    ctx = request.app.state.ctx
    try:
        document = yaml.safe_load((await request.body()).decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as err:
        return _error("invalid", f"not YAML: {str(err).splitlines()[0]}"[:300], 422)
    if not isinstance(document, dict) or document.get("agent_house_export") != EXPORT_VERSION:
        return _error("invalid", f"not an Agent House export (agent_house_export: {EXPORT_VERSION})", 422)
    unknown = sorted(set(document) - {"agent_house_export", "agents", "policy", "settings", "active_profile"})
    if unknown:
        return _error("invalid", f"unknown key {unknown[0]!r}", 422)
    try:
        raw_agents = document.get("agents") or []
        if not isinstance(raw_agents, list):
            raise ConfigError("agents must be a list")
        agents = []
        for index, payload in enumerate(raw_agents):
            try:
                agents.append(parse_agent(payload))
            except ConfigError as err:
                raise ConfigError(f"agents.{index}: {err}") from None
        if len({a.id for a in agents}) != len(agents):
            raise ConfigError("agents: an id appears twice")
        policy = parse_policy(document["policy"]) if document.get("policy") is not None else None
        runtime = RuntimeSettings(**document["settings"]) if document.get("settings") is not None else None
    except ConfigError as err:
        return _error("invalid", str(err), 422)
    except (ValidationError, TypeError) as err:
        first = err.errors()[0] if isinstance(err, ValidationError) else {"loc": (), "msg": str(err)}
        where = ".".join(str(p) for p in first["loc"])
        return _error("invalid", f"settings.{where}: {first['msg']}", 422)
    forced = [a.id for a in agents if a.mode != "shadow"]
    for agent in agents:
        ctx.agents.save(agent.model_copy(update={"mode": "shadow"}))
    if policy is not None:
        ctx.safety.save_policy(policy)
    if runtime is not None and runtime != ctx.runtime:
        save_runtime(ctx.settings.data_dir, runtime)
        ctx.runtime = ctx.llm.runtime = ctx.agents_rt.settings = runtime
    ctx.gate.reload()
    ctx.feed.add(
        "system",
        f"Config imported: {len(agents)} agents (all in shadow mode)"
        + (", policy" if policy is not None else "")
        + (", settings" if runtime is not None else ""),
        level="warning",
    )
    return {
        "agents": [a.id for a in agents],
        "set_to_shadow": forced,
        "policy": policy is not None,
        "settings": runtime is not None,
    }
