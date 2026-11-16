"""Activity and safety API: runs (traces of the orchestrator's model calls), the policy, the action
log, T2 approvals and the owner's inbox. Contract: contracts/openapi.yaml."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..core.actuator import ApprovalConflict
from ..core.safety import ConfigError, check_whitelist, parse_policy

router = APIRouter(prefix="/api")
VERDICTS = ("executed", "shadow", "denied", "pending", "approved", "expired", "failed")
RUN_STATES = ("queued", "running", "ok", "error", "timeout", "dropped", "escalated")


def _error(code: str, detail: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status_code=status)


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
    return request.app.state.ctx.runs.list(agent=agent, status=status, limit=limit, before=before)


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    run = request.app.state.ctx.runs.get(run_id)
    return run or _error("not_found", f"no run {run_id!r}", 404)


# --- safety ---------------------------------------------------------------------------------


@router.get("/policy")
async def get_policy(request: Request) -> dict:
    return request.app.state.ctx.safety.policy


@router.put("/policy")
async def put_policy(request: Request):
    ctx = request.app.state.ctx
    try:
        body = await request.json()
    except ValueError:
        body = None
    try:
        check_whitelist(body, ctx.safety.t3_whitelist)
        policy = parse_policy(body)
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
    return request.app.state.ctx.inbox.list()
