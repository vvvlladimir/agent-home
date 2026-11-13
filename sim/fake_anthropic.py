"""Fake Anthropic Messages API. Rejects parameters current Claude models reject, so tests catch them."""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    429: "rate_limit_error",
    500: "api_error",
    529: "overloaded_error",
}


def error(status: int, message: str, typ: str | None = None) -> JSONResponse:
    t = typ or ERROR_TYPES.get(status, "api_error")
    return JSONResponse({"type": "error", "error": {"type": t, "message": message}}, status_code=status)


class AnthropicSim:
    def __init__(self):
        self.api_key = "sk-ant-sim"
        self.script: list[dict] = []
        self.requests: list[dict] = []
        self._msg = self._tool = 0

    def reset(self) -> None:
        self.script.clear()
        self.requests.clear()


def rejected_param(body: dict) -> str | None:
    for k in ("temperature", "top_p", "top_k"):
        if k in body:
            return f"`{k}` is not supported for this model."
    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        if "budget_tokens" in thinking:
            return "`thinking.budget_tokens` is not supported for this model."
        if thinking.get("type") == "disabled":
            return "`thinking.type=disabled` is not supported for this model."
    tc = body.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") in ("any", "tool"):
        return f"`tool_choice.type={tc['type']}` is not supported for this model."
    return None


def create_app(sim: AnthropicSim | None = None) -> FastAPI:
    sim = sim or AnthropicSim()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sim = sim

    @app.post("/v1/messages")
    async def messages(request: Request):
        body = await request.json()
        h = request.headers
        sim.requests.append(
            {
                "body": body,
                "query": dict(request.query_params),
                "headers": {
                    k: h.get(k) for k in ("x-api-key", "anthropic-version", "anthropic-beta") if k in h
                },
            }
        )
        # SDK 2.x sends the key as a bearer token; older clients use x-api-key
        if sim.api_key not in (h.get("x-api-key"), h.get("authorization", "").removeprefix("Bearer ")):
            return error(401, "invalid x-api-key")
        if bad := rejected_param(body):
            return error(400, bad)
        step = sim.script.pop(0) if sim.script else {"text": "OK."}
        if step.get("delay"):
            await asyncio.sleep(step["delay"])
        if step.get("hang"):
            await asyncio.sleep(step["hang"])
        if step.get("status"):
            return error(step["status"], "simulated failure")
        content, stop = [], "end_turn"
        if step.get("refusal"):
            stop = "refusal"
        elif "tool_use" in step:
            stop = "tool_use"
            for t in step["tool_use"]:
                sim._tool += 1
                content.append(
                    {
                        "type": "tool_use",
                        "id": f"toolu_sim_{sim._tool}",
                        "name": t["name"],
                        "input": t.get("input", {}),
                    }
                )
        else:
            content.append({"type": "text", "text": step.get("text", "OK.")})
        stop = step.get("stop_reason", stop)
        sim._msg += 1
        return {
            "id": f"msg_sim_{sim._msg}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model"),
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {
                "input_tokens": max(1, len(json.dumps(body)) // 4),
                "output_tokens": max(1, len(json.dumps(content)) // 4),
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }

    @app.post("/_sim/script")
    async def add_script(request: Request):
        body = await request.json()
        sim.script.extend(body if isinstance(body, list) else body.get("steps", [body]))
        return {"queued": len(sim.script)}

    @app.get("/_sim/requests")
    async def get_requests():
        return sim.requests

    @app.post("/_sim/reset")
    async def reset():
        sim.reset()
        return {"ok": True}

    return app
