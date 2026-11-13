"""M3 API: agents, runs (traces), policy, actions, approvals, inbox. Contract: contracts/openapi.yaml."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from ..core.actuator import ApprovalConflict
from ..core.agents import AgentConfig, ConfigError, parse_agent, parse_policy
from ..core.runtime import WakeRefused

router = APIRouter(prefix="/api")
VERDICTS = ("executed", "shadow", "denied", "pending", "approved", "expired", "failed")
RUN_STATES = ("queued", "running", "ok", "error", "timeout", "dropped", "escalated")
REFUSED_STATUS = {"not_found": 404, "kill_switch": 409, "disabled": 409, "no_context": 409}


def _error(code: str, detail: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status_code=status)


def _refused(err: WakeRefused) -> JSONResponse:
    return _error(err.code, str(err), REFUSED_STATUS.get(err.code, 409))


async def _body(request: Request) -> object:
    try:
        return await request.json()
    except ValueError:
        return None


def _summary(ctx, agent: AgentConfig) -> dict:
    last = ctx.agents_rt.list_runs(agent=agent.id, limit=1)
    return {
        "id": agent.id,
        "name": agent.name,
        "room": agent.room,
        "enabled": agent.enabled,
        "mode": agent.mode,
        "effective_mode": ctx.agents_rt.effective_mode(agent),
        "entity_count": len(ctx.agents_rt.scope(agent)),
        "last_run": last[0] if last else None,
        "wakes_last_hour": ctx.gate.wakes_last_hour(agent.id),
    }


# --- agents ---------------------------------------------------------------------------------


@router.get("/agents")
async def list_agents(request: Request) -> list[dict]:
    ctx = request.app.state.ctx
    return [_summary(ctx, agent) for agent in ctx.agents.list()]


def _save(ctx, agent: AgentConfig, verb: str) -> None:
    ctx.agents.save(agent)
    ctx.gate.reload()
    level = "warning" if agent.mode == "live" else "info"
    ctx.feed.add("system", f"Agent {agent.id} {verb} (mode {agent.mode})", level=level, agent_id=agent.id)


@router.post("/agents")
async def create_agent(request: Request):
    ctx = request.app.state.ctx
    try:
        agent = parse_agent(await _body(request))
    except ConfigError as err:
        return _error("invalid", str(err), 422)
    if ctx.agents.get(agent.id) is not None:
        return _error("invalid", f"agent {agent.id} already exists", 422)
    _save(ctx, agent, "created")
    return JSONResponse(agent.dump(), status_code=201)


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, request: Request):
    agent = request.app.state.ctx.agents.get(agent_id)
    return agent.dump() if agent else _error("not_found", f"no agent {agent_id!r}", 404)


@router.put("/agents/{agent_id}")
async def update_agent(agent_id: str, request: Request):
    ctx = request.app.state.ctx
    if ctx.agents.get(agent_id) is None:
        return _error("not_found", f"no agent {agent_id!r}", 404)
    try:
        agent = parse_agent(await _body(request))
    except ConfigError as err:
        return _error("invalid", str(err), 422)
    if agent.id != agent_id:
        return _error("invalid", "the id of an agent cannot be changed", 422)
    _save(ctx, agent, "updated")
    return agent.dump()


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, request: Request):
    ctx = request.app.state.ctx
    if not ctx.agents.delete(agent_id):
        return _error("not_found", f"no agent {agent_id!r}", 404)
    ctx.store.execute("DELETE FROM scheduled_wakes WHERE agent_id=?", (agent_id,))
    ctx.store.execute("DELETE FROM memory WHERE agent_id=?", (agent_id,))
    ctx.gate.reload()
    ctx.feed.add("system", f"Agent {agent_id} deleted", agent_id=agent_id)
    return Response(status_code=204)


class WakeBody(BaseModel):
    reason: str | None = Field(default=None, max_length=300)


@router.post("/agents/{agent_id}/wake")
async def wake_agent(agent_id: str, request: Request, body: WakeBody | None = None):
    ctx = request.app.state.ctx
    reason = (body.reason if body else None) or "Manual wake by the owner: check your room."
    try:
        run_id = ctx.agents_rt.wake(agent_id, source="manual", reason=reason)
    except WakeRefused as err:
        return _refused(err)
    return JSONResponse({"run_id": run_id}, status_code=202)


@router.post("/agents/{agent_id}/prompt-preview")
async def preview_prompt(agent_id: str, request: Request):
    ctx = request.app.state.ctx
    payload = await _body(request)
    try:
        agent = parse_agent(payload) if payload else ctx.agents.get(agent_id)
    except ConfigError as err:
        return _error("invalid", str(err), 422)
    if agent is None:
        return _error("not_found", f"no agent {agent_id!r}", 404)
    context = ctx.agents_rt.build_context(agent, "(the wake reason goes here)")
    keys = ("system", "user", "tokens_estimate", "budget", "entities_included", "entities_dropped")
    return {key: context[key] for key in keys}


# --- runs -----------------------------------------------------------------------------------


@router.get("/runs")
async def list_runs(
    request: Request,
    agent: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    before: str | None = None,
):
    if status is not None and status not in RUN_STATES:
        return _error("invalid", f"status must be one of {', '.join(RUN_STATES)}", 422)
    return request.app.state.ctx.agents_rt.list_runs(agent=agent, status=status, limit=limit, before=before)


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    run = request.app.state.ctx.agents_rt.get_run(run_id)
    return run or _error("not_found", f"no run {run_id!r}", 404)


@router.post("/runs/{run_id}/replay")
async def replay_run(run_id: str, request: Request):
    try:
        return JSONResponse({"run_id": request.app.state.ctx.agents_rt.replay(run_id)}, status_code=202)
    except WakeRefused as err:
        return _refused(err)


@router.get("/runs/{run_id}/testcase")
async def run_as_testcase(run_id: str, request: Request):
    try:
        return Response(request.app.state.ctx.agents_rt.testcase(run_id), media_type="text/yaml")
    except WakeRefused as err:
        return _refused(err)


# --- safety ---------------------------------------------------------------------------------


@router.get("/policy")
async def get_policy(request: Request) -> dict:
    return request.app.state.ctx.safety.policy


@router.put("/policy")
async def put_policy(request: Request):
    ctx = request.app.state.ctx
    try:
        policy = parse_policy(await _body(request))
    except ConfigError as err:
        return _error("invalid", str(err), 422)
    return ctx.safety.save_policy(policy)


@router.get("/actions")
async def list_actions(
    request: Request,
    agent: str | None = None,
    verdict: str | None = None,
    limit: int = Query(100, ge=1, le=500),
):
    if verdict is not None and verdict not in VERDICTS:
        return _error("invalid", f"verdict must be one of {', '.join(VERDICTS)}", 422)
    return request.app.state.ctx.actuator.list(agent=agent, verdict=verdict, limit=limit)


class ApprovalBody(BaseModel):
    approve: bool


@router.post("/approvals/{action_id}")
async def decide_approval(action_id: str, body: ApprovalBody, request: Request):
    try:
        return await request.app.state.ctx.actuator.decide_approval(action_id, body.approve)
    except ApprovalConflict as err:
        return _error("conflict", str(err), 409)


@router.get("/inbox")
async def list_inbox(request: Request) -> list[dict]:
    return request.app.state.ctx.agents_rt.inbox()
