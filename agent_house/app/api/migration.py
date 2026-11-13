"""M5 API: the owner's existing automations, their proposed replacements and the parity report.
Contract: contracts/openapi.yaml (tag `migration`)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..core.lifecycle import LifecycleError
from ..core.migration import MigrationError

router = APIRouter(prefix="/api/migration")
HTTP = {"not_found": 404, "conflict": 409, "kill_switch": 409, "ha_error": 502, "bad_request": 400}


def _error(code: str, detail: str) -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status_code=HTTP.get(code, 409))


@router.post("/import")
async def import_originals(request: Request):
    return await request.app.state.ctx.migration.import_originals()


@router.get("/originals")
async def list_originals(request: Request):
    return request.app.state.ctx.migration.list()


@router.post("/originals/{original_id}/propose")
async def propose_replacement(original_id: str, request: Request):
    try:
        run_id = request.app.state.ctx.migration.propose(original_id)
    except MigrationError as err:
        return _error(err.code, str(err))
    return JSONResponse({"run_id": run_id}, status_code=202)


@router.get("/originals/{original_id}/parity")
async def get_parity(original_id: str, request: Request, days: int = 7):
    if not 1 <= days <= 30:
        return _error("bad_request", "days must be 1 to 30")
    try:
        return await request.app.state.ctx.migration.parity(original_id, days)
    except (MigrationError, LifecycleError) as err:
        return _error(err.code, str(err))


@router.put("/originals/{original_id}/enabled")
async def set_original_enabled(original_id: str, request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = None
    enabled = body.get("enabled") if isinstance(body, dict) else None
    if not isinstance(enabled, bool):
        return _error("bad_request", "`enabled` must be true or false")
    try:
        return await request.app.state.ctx.migration.set_enabled(original_id, enabled)
    except MigrationError as err:
        return _error(err.code, str(err))
