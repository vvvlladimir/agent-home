"""M4 API: proposals (automation drafts) and the automations Agent House deployed.
Contract: contracts/openapi.yaml (tag `automations`)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..core.lifecycle import LifecycleError
from ..core.orchestrator import OrchestratorError
from ..ha.client import NotOurs

router = APIRouter(prefix="/api")
STATUSES = ("draft", "blocked", "approved", "deployed", "rejected", "superseded", "rolled_back")
HTTP = {
    "not_found": 404, "conflict": 409, "kill_switch": 409, "blocked": 409, "ha_error": 502,
    "declined": 422, "failed": 422, "unavailable": 503, "budget": 503, "bad_request": 400,
}  # fmt: skip


def _error(code: str, detail: str) -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status_code=HTTP.get(code, 409))


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _text(body: dict, key: str, limit: int = 20000) -> str | None:
    value = body.get(key)
    return value[:limit] if isinstance(value, str) and value.strip() else None


# --- proposals --------------------------------------------------------------------------------


@router.get("/proposals")
async def list_proposals(request: Request, status: str | None = None):
    if status and status not in STATUSES:
        return _error("bad_request", f"status must be one of {', '.join(STATUSES)}")
    return request.app.state.ctx.lifecycle.list(status)


@router.post("/proposals")
async def create_proposal(request: Request):
    """Either `yaml` (the owner's own draft) or `request` (the orchestrator drafts it)."""
    ctx = request.app.state.ctx
    body = await _body(request)
    yaml_text, wish = _text(body, "yaml"), _text(body, "request", 2000)
    ha_id = _text(body, "ha_id", 80)
    if bool(yaml_text) == bool(wish):
        return _error("bad_request", "send either `yaml` or `request`")
    try:
        if yaml_text:
            proposal = await ctx.lifecycle.create(
                yaml_text,
                title=_text(body, "title", 120),
                rationale=_text(body, "rationale", 1000),
                ha_id=ha_id,
            )
        else:
            room = _text(body, "room", 64)
            proposal = await ctx.orchestrator.draft(wish, room=room, ha_id=ha_id)
    except (LifecycleError, OrchestratorError) as err:
        return _error(err.code, str(err))
    except NotOurs as err:
        return _error("not_found", str(err))
    return JSONResponse(proposal, status_code=201)


@router.get("/proposals/{proposal_id}")
async def get_proposal(proposal_id: str, request: Request):
    try:
        return request.app.state.ctx.lifecycle.get(proposal_id)
    except LifecycleError as err:
        return _error(err.code, str(err))


@router.put("/proposals/{proposal_id}")
async def edit_proposal(proposal_id: str, request: Request):
    text = _text(await _body(request), "yaml")
    if text is None:
        return _error("bad_request", "`yaml` is required")
    try:
        return await request.app.state.ctx.lifecycle.edit(proposal_id, text)
    except LifecycleError as err:
        return _error(err.code, str(err))


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: str, request: Request):
    try:
        return await request.app.state.ctx.lifecycle.approve(proposal_id)
    except LifecycleError as err:
        return _error(err.code, str(err))


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: str, request: Request):
    try:
        return request.app.state.ctx.lifecycle.reject(proposal_id, _text(await _body(request), "note", 500))
    except LifecycleError as err:
        return _error(err.code, str(err))


@router.post("/proposals/{proposal_id}/evidence")
async def proposal_evidence(proposal_id: str, request: Request):
    days = (await _body(request)).get("days", 7)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 30:
        return _error("bad_request", "days must be 1 to 30")
    try:
        return await request.app.state.ctx.lifecycle.evidence(proposal_id, days)
    except LifecycleError as err:
        return _error(err.code, str(err))
    except Exception as err:  # noqa: BLE001 - history unavailable is an answer, not a 500
        return _error("ha_error", f"history: {type(err).__name__}: {err}"[:300])


# --- deployed automations -------------------------------------------------------------------


@router.get("/automations")
async def list_deployed(request: Request):
    return request.app.state.ctx.lifecycle.deployed()


@router.get("/automations/{ha_id}")
async def get_deployed(ha_id: str, request: Request):
    try:
        return request.app.state.ctx.lifecycle.get_deployed(ha_id)
    except LifecycleError as err:
        return _error(err.code, str(err))


@router.delete("/automations/{ha_id}")
async def remove_deployed(ha_id: str, request: Request):
    try:
        await request.app.state.ctx.lifecycle.remove(ha_id)
    except LifecycleError as err:
        return _error(err.code, str(err))
    return Response(status_code=204)


@router.post("/automations/{ha_id}/rollback")
async def rollback_deployed(ha_id: str, request: Request):
    version = (await _body(request)).get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        return _error("bad_request", "`version` must be an integer")
    try:
        return await request.app.state.ctx.lifecycle.rollback(ha_id, version)
    except LifecycleError as err:
        return _error(err.code, str(err))


@router.put("/automations/{ha_id}/enabled")
async def set_deployed_enabled(ha_id: str, request: Request):
    enabled = (await _body(request)).get("enabled")
    if not isinstance(enabled, bool):
        return _error("bad_request", "`enabled` must be true or false")
    try:
        return await request.app.state.ctx.lifecycle.set_enabled(ha_id, enabled)
    except LifecycleError as err:
        return _error(err.code, str(err))
