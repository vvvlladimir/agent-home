"""Fake OpenAI Responses API (`POST /v1/responses`): the subset the orchestrator uses.

Rejects what the GPT-6 models reject for our use (sampling parameters, chat-style `messages`,
tools nested under `function`) so tests catch regressions. Script steps:
`{"function_calls": [{"name": ..., "arguments": {...}}]}`, `{"text": ...}`, `{"status": 500}`,
`{"refusal": "..."}`, optionally `"model"` (the model that answered).
"""

from __future__ import annotations

import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class OpenAISim:
    def __init__(self) -> None:
        self.api_key = "sk-proj-sim"
        self.script: list[dict] = []
        self.requests: list[dict] = []
        self._n = 0

    def reset(self) -> None:
        self.script.clear()
        self.requests.clear()


def error(status: int, message: str, typ: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": typ, "param": None, "code": None}}, status_code=status
    )


def rejected_param(body: dict) -> str | None:
    for key in ("temperature", "top_p", "messages", "functions"):
        if key in body:
            return f"Unsupported parameter: '{key}'."
    for tool in body.get("tools") or []:
        if tool.get("type") == "function" and ("function" in tool or "name" not in tool):
            return "Missing required parameter: 'tools[0].name'."
    for item in body.get("input") or []:
        if isinstance(item, dict) and item.get("role") == "tool":
            return (
                "Invalid value: 'tool'. Supported values are: 'assistant', 'system', 'developer', and 'user'."
            )
    return None


def create_app(sim: OpenAISim | None = None) -> FastAPI:
    sim = sim or OpenAISim()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sim = sim

    @app.post("/v1/responses")
    async def responses(request: Request):
        body = await request.json()
        sim.requests.append(
            {"body": body, "headers": {"authorization": request.headers.get("authorization")}}
        )
        if request.headers.get("authorization") != f"Bearer {sim.api_key}":
            return error(401, "Incorrect API key provided.", "invalid_api_key")
        if bad := rejected_param(body):
            return error(400, bad)
        step = sim.script.pop(0) if sim.script else {"text": "OK."}
        if step.get("status"):
            return error(step["status"], "simulated failure", "server_error")
        sim._n += 1
        output: list[dict] = [
            {"type": "reasoning", "id": f"rs_{sim._n}", "summary": [], "encrypted_content": "gAAAA-sim"}
        ]
        for n, call in enumerate(step.get("function_calls") or []):
            output.append(
                {
                    "type": "function_call",
                    "id": f"fc_{sim._n}_{n}",
                    "call_id": f"call_{sim._n}_{n}",
                    "name": call["name"],
                    "arguments": json.dumps(call.get("arguments") or {}),
                    "status": "completed",
                }
            )
        if "text" in step or "refusal" in step:
            part = (
                {"type": "refusal", "refusal": step["refusal"]}
                if "refusal" in step
                else {"type": "output_text", "text": step["text"], "annotations": []}
            )
            output.append(
                {
                    "type": "message",
                    "id": f"msg_{sim._n}",
                    "role": "assistant",
                    "status": "completed",
                    "content": [part],
                }
            )
        tokens_in = max(1, len(json.dumps(body)) // 4)
        tokens_out = max(1, len(json.dumps(output)) // 4)
        return {
            "id": f"resp_{sim._n}",
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": step.get("model", body.get("model")),
            "output": output,
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": body.get("tools") or [],
            "error": None,
            "incomplete_details": None,
            "usage": {
                "input_tokens": tokens_in,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": tokens_out,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": tokens_in + tokens_out,
            },
        }

    return app
