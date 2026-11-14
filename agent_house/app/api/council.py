"""M5 API: the agent council (house mode, shared board, ownership) and the history findings the
daily review turns into proposals. Contract: contracts/openapi.yaml (tag `council`)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..core.orchestrator import OrchestratorError
from ..ha.client import HAError

router = APIRouter(prefix="/api")
HTTP = {"not_found": 404, "conflict": 409, "kill_switch": 409, "ha_error": 502, "bad_request": 400}


def _error(code: str, detail: str) -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status_code=HTTP.get(code, 409))


@router.get("/council")
async def get_council(request: Request):
    ctx = request.app.state.ctx
    return {
        **ctx.council.status(),
        "review": {
            "at": ctx.runtime.daily_review_at or None,
            "running": ctx.review.running,
            "last": ctx.store.get("review_last"),
        },
        "last_scan": ctx.store.get("insights_last_scan"),
        "first_look": ctx.store.get("habits_first_look"),
    }


@router.get("/council/owner/{entity_id}")
async def get_owner(entity_id: str, request: Request):
    ctx = request.app.state.ctx
    if entity_id not in ctx.ha.states:
        return _error("not_found", f"{entity_id} is unknown to Home Assistant")
    return {"entity_id": entity_id, "owner": ctx.council.ownership.owner_of(entity_id),
            "area": ctx.ha.registry.area_of(entity_id)}  # fmt: skip


@router.post("/council/mode-helper")
async def create_mode_helper(request: Request):
    ctx = request.app.state.ctx
    try:
        entity_id = await ctx.ha.create_mode_helper()
    except HAError as err:
        code = "conflict" if "exists" in str(err) else "ha_error"
        return _error(code, str(err)[:300])
    ctx.feed.add("council", f"Created {entity_id}", level="warning")
    return JSONResponse({"entity_id": entity_id}, status_code=201)


@router.post("/council/review")
async def run_review(request: Request):
    ctx = request.app.state.ctx
    if ctx.safety.kill_engaged:
        return _error("kill_switch", "the kill switch is engaged")
    if ctx.review.running:
        return _error("conflict", "a review is already running")

    async def job() -> None:
        try:
            await ctx.review.run()
        except OrchestratorError as err:
            ctx.feed.add("council", f"Daily review failed: {err}", level="warning")

    ctx.orchestrator.spawn(job(), "review")
    return JSONResponse({"status": "started"}, status_code=202)


@router.post("/council/scan")
async def scan_history(request: Request):
    ctx = request.app.state.ctx
    try:
        result = await ctx.insights.scan()
    except HAError as err:
        return _error("ha_error", f"the logbook is unavailable: {err}"[:300])
    return result


@router.get("/findings")
async def list_findings(request: Request, status: str | None = None):
    return request.app.state.ctx.insights.list(status)


@router.post("/findings/{finding_id}/draft")
async def draft_finding(finding_id: str, request: Request):
    ctx = request.app.state.ctx
    finding = ctx.insights.get(finding_id)
    if finding is None:
        return _error("not_found", f"no finding {finding_id!r}")
    if finding["status"] not in ("new", "failed"):
        return _error("conflict", f"the finding is {finding['status']}")
    if ctx.safety.kill_engaged:
        return _error("kill_switch", "the kill switch is engaged")
    ctx.insights.set_status(finding_id, "new")

    async def job() -> None:
        await ctx.review.draft(finding_id)

    ctx.orchestrator.spawn(job(), f"finding-{finding_id[:40]}")
    return JSONResponse({"status": "drafting"}, status_code=202)


@router.post("/findings/{finding_id}/dismiss")
async def dismiss_finding(finding_id: str, request: Request):
    ctx = request.app.state.ctx
    if ctx.insights.get(finding_id) is None:
        return _error("not_found", f"no finding {finding_id!r}")
    return ctx.insights.set_status(finding_id, "dismissed")


# --- D27 house manager, D28 accuracy metrics --------------------------------------------------
@router.get("/manager")
async def get_manager(request: Request):
    return request.app.state.ctx.manager.status()


@router.post("/manager/wake")
async def wake_manager(request: Request):
    """Wake the house manager now (owner or tester). Works with manager_enabled off too, but then
    it may only assign tasks, propose and report: the entities it owns join its scope only when on."""
    ctx = request.app.state.ctx
    try:
        body = await request.json()
    except ValueError:
        body = {}
    reason = str((body or {}).get("reason") or "woken by hand").strip()[:500] or "woken by hand"
    result = ctx.manager.request(f"manual: {reason}", force=True)
    if result["status"] == "refused":
        code = "kill_switch" if "kill" in result["detail"] else "conflict"
        return _error(code, result["detail"])
    result["enabled"] = ctx.manager.enabled
    result["note"] = (
        "The manager is on: this run may also switch the entities it owns (in its mode)."
        if ctx.manager.enabled
        else "The manager is off: this one run may look, give room agents tasks, propose drafts and "
        "report to you, but switches nothing. It counts toward the hourly wake limit."
    )
    return JSONResponse(result, status_code=202)


@router.get("/metrics")
async def get_metrics(request: Request, days: int = 7):
    ctx = request.app.state.ctx
    try:
        return await ctx.metrics.compute(max(1, min(int(days), 30)))
    except HAError as err:
        return _error("ha_error", f"the logbook is unavailable: {err}"[:300])
