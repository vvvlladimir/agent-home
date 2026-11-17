"""Developer tools (D40): one read-only snapshot of the add-on's internals, `GET api/dev`.
Contract: contracts/openapi.yaml `DevSnapshot`. Built from the status functions the other pages use;
secrets are masked (`Settings.masked`) and no model prompt or answer is in it.
"""

from __future__ import annotations

from datetime import UTC

from fastapi import APIRouter, Request

from .. import __version__
from ..config import SECRET_FIELDS
from ..core import clock
from ..core.policy import TIERS, resolve_tier
from .routes import _learner_brief

router = APIRouter(prefix="/api")
RECENT = 20
TABLES = ("runs", "actions", "inbox", "proposals", "deployed", "findings", "originals", "feed", "kv")
MODEL_PARTS = ("habits", "routines", "dependencies", "corrections", "timeouts", "usual_states",
               "event_relevance", "health", "modes", "asks", "live")  # fmt: skip


def _learner(ctx) -> dict:
    learner = ctx.learner
    model = ctx.store.get("learner_model") or None
    return {
        **learner.status(),
        "targets": learner.targets(),
        "facts": learner.facts(limit=50),
        # what the Jetson came up with, as received (contracts/schemas/house_model.schema.json)
        "house_model": {
            k: model.get(k)
            for k in ("schema_version", "trained_at", "duration_s", "history", *MODEL_PARTS)
            if k in model
        }
        if model
        else None,  # fmt: skip
        "live_suggestions": ctx.store.get("learner_actions") or [],
        "tables": {
            "usual_states": len(ctx.store.get("learner_usual_states") or {}),
            "habits_source": (ctx.store.get("insights_last_scan") or {}).get("habits", {}).get("source"),
        },
    }


def _manager(ctx) -> dict:
    runs = [r for r in ctx.runs.list(agent="orchestrator", limit=200) if r["wake_source"] == "manage"]
    return {
        **ctx.manager.status(),
        "last_sweep": next((w["last"] for w in ctx.council.wakes() if w["id"] == "manager_sweep"), None),
        "runs": [
            {k: r[k] for k in ("id", "started", "status", "provider", "tokens_in", "tokens_out", "shadow")}
            for r in runs[:RECENT]
        ],  # fmt: skip
    }


def _orchestrator(ctx) -> dict:
    orch = ctx.orchestrator
    return {
        **orch.status(),
        "providers": [p.name for p in orch.providers()],
        "redact_cloud": bool(ctx.settings.redact_cloud),
        "last_calls": list(orch.calls)[::-1],
    }


def _policy(ctx) -> dict:
    policy = ctx.safety.policy
    tiers = dict.fromkeys(TIERS, 0)
    for entity_id in ctx.ha.states:
        tiers[resolve_tier(entity_id, policy)] += 1
    keys = ("id", "ts", "agent_id", "domain", "service", "entity_ids", "tier", "verdict", "reason")
    return {
        "mode": ctx.safety.mode,
        "kill_switch": ctx.safety.kill,
        "tiers": tiers,
        "t3_whitelist": ctx.safety.t3_whitelist,
        "pending_approvals": ctx.actuator.pending_count(),
        "recent": [{k: a[k] for k in keys} for a in ctx.actuator.list(limit=RECENT)],
    }


def _store(ctx) -> dict:
    counts = {t: ctx.store.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in TABLES}  # noqa: S608
    return {"path": ctx.store.path, "rows": counts}


def _scrub(value, secrets: list[str]):
    """Error texts from providers and HA are shown as they came; a key echoed in one is masked here."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "***")
        return value
    if isinstance(value, list):
        return [_scrub(v, secrets) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, secrets) for k, v in value.items()}
    return value


@router.get("/dev")
async def get_dev(request: Request) -> dict:
    ctx = request.app.state.ctx
    secrets = [v for k in SECRET_FIELDS if len(v := str(getattr(ctx.settings, k, "") or "")) >= 4]
    return _scrub(_snapshot(ctx), secrets)


def _snapshot(ctx) -> dict:
    learner = ctx.learner
    return {
        "version": __version__,
        "time": clock.now(UTC).isoformat(),
        "learner": _learner(ctx),
        "manager": _manager(ctx),
        "orchestrator": _orchestrator(ctx),
        "policy": _policy(ctx),
        "council": ctx.council.status(),
        "store": _store(ctx),
        "ha": {
            **ctx.ha.status(),
            "forwarder": {
                **_learner_brief(ctx),
                "buffered": len(learner.buffer),
                "dropped": learner.dropped,
                "pushed": learner.pushed,
            },
        },
        "config": {"options": ctx.settings.masked(), "runtime": ctx.runtime.model_dump(mode="json")},
    }
