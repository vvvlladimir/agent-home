"""Fake llama-server (OpenAI-compatible chat completions) with a scriptable response queue."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

_SEP = r"\W{1,4}(?:(?:is|state)\W{1,4})?"
_MOTION = re.compile(rf"(binary_sensor\.\w*motion\w*){_SEP}on\b")
_LIGHT = re.compile(rf"(light\.\w+){_SEP}off\b")


def _text_of(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""


class LLMSim:
    def __init__(self):
        self.script: list[dict] = []
        self.requests: list[dict] = []
        self.loading = False
        self.latency = 0.0
        self.tokens_per_second = 20.0
        self.model = "qwen35-4b-balanced"
        self.api_key: str | None = None  # set it to require `Authorization: Bearer <key>` like --api-key
        self._n = 0

    def reset(self) -> None:
        self.script.clear()
        self.requests.clear()
        self.loading, self.latency = False, 0.0

    def default_policy(self, request: dict) -> dict:
        msgs = request.get("messages") or []
        tools = {(t.get("function") or {}).get("name") for t in request.get("tools") or []}
        if msgs and msgs[-1].get("role") == "tool":
            return {"text": "Done."}
        user = next((_text_of(m) for m in reversed(msgs) if m.get("role") == "user"), "")
        motion, light = _MOTION.search(user), _LIGHT.search(user)
        if "call_service" in tools and motion and light:
            args = {"domain": "light", "service": "turn_on", "entity_id": light.group(1)}
            return {"tool_calls": [{"name": "call_service", "arguments": args}]}
        if "report" in tools:
            return {
                "tool_calls": [
                    {"name": "report", "arguments": {"severity": "info", "summary": "Nothing to do."}}
                ]
            }
        return {"text": "OK."}

    def next_id(self) -> int:
        self._n += 1
        return self._n


def _message(step: dict, sim: LLMSim) -> tuple[dict, str]:
    """Build the assistant message and finish_reason for a scripted step."""
    if "tool_calls" in step or "raw_arguments" in step:
        calls = step.get("tool_calls") or [{"name": step.get("name", "noop"), "arguments": None}]
        out = []
        for c in calls:
            args = (
                step.get("raw_arguments") if "raw_arguments" in step else json.dumps(c.get("arguments") or {})
            )
            out.append(
                {
                    "id": f"call_sim_{sim.next_id()}",
                    "type": "function",
                    "function": {"name": c["name"], "arguments": args},
                }
            )
        return {"role": "assistant", "content": None, "tool_calls": out}, "tool_calls"
    if "content_tool_call" in step:
        txt = f"<tool_call>{json.dumps(step['content_tool_call'])}</tool_call>"
        return {"role": "assistant", "content": txt}, "stop"
    return {"role": "assistant", "content": step.get("text", "OK.")}, "stop"


def _chunks(msg: dict, finish: str) -> list[dict]:
    deltas: list[dict] = [{"role": "assistant", "content": ""}]
    if msg.get("tool_calls"):
        for i, tc in enumerate(msg["tool_calls"]):
            a = tc["function"]["arguments"]
            mid = max(1, len(a) // 2)
            deltas.append(
                {
                    "tool_calls": [
                        {
                            "index": i,
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["function"]["name"], "arguments": a[:mid]},
                        }
                    ]
                }
            )
            deltas.append({"tool_calls": [{"index": i, "function": {"arguments": a[mid:]}}]})
    else:
        words = re.findall(r"\S+\s*", msg["content"] or "")
        deltas += [{"content": w} for w in words]
    out = [{"choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
    out.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    return out


def create_app(sim: LLMSim | None = None) -> FastAPI:
    sim = sim or LLMSim()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sim = sim

    def loading_resp() -> JSONResponse:
        err = {"message": "Loading model", "type": "unavailable_error", "code": 503}
        return JSONResponse({"error": err}, status_code=503)

    @app.get("/health")
    async def health():
        return loading_resp() if sim.loading else {"status": "ok"}

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": sim.model, "object": "model", "owned_by": "llamacpp"}]}

    @app.get("/props")
    async def props():
        return {
            "default_generation_settings": {"n_ctx": 8192},
            "model_path": f"/models/{sim.model}.gguf",
            "total_slots": 1,
        }

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        if sim.api_key and request.headers.get("authorization") != f"Bearer {sim.api_key}":
            err = {"message": "Invalid API Key", "type": "authentication_error", "code": 401}
            return JSONResponse({"error": err}, status_code=401)
        if sim.loading:
            return loading_resp()
        body = await request.json()
        sim.requests.append(body)
        step = sim.script.pop(0) if sim.script else sim.default_policy(body)
        if sim.latency or step.get("delay"):
            await asyncio.sleep(step.get("delay", sim.latency))
        if step.get("hang"):
            await asyncio.sleep(step["hang"])
        if step.get("garbage"):
            return Response("<html>not json at all", media_type="text/html")
        if step.get("status"):
            return JSONResponse(
                {"error": {"message": "simulated failure", "type": "server_error", "code": step["status"]}},
                status_code=step["status"],
            )
        msg, finish = _message(step, sim)
        n_prompt = max(1, len(json.dumps(body)) // 4)
        n_out = max(1, len(json.dumps(msg["tool_calls"]) if msg.get("tool_calls") else msg["content"]) // 4)
        tps = sim.tokens_per_second
        timings = {
            "prompt_n": n_prompt,
            "prompt_ms": n_prompt / (tps * 10) * 1000,
            "prompt_per_second": tps * 10,
            "predicted_n": n_out,
            "predicted_ms": n_out / tps * 1000,
            "predicted_per_second": tps,
            "cache_n": 0,
        }
        usage = {"prompt_tokens": n_prompt, "completion_tokens": n_out, "total_tokens": n_prompt + n_out}
        base: dict[str, Any] = {
            "id": f"chatcmpl-sim-{sim.next_id()}",
            "created": int(time.time()),
            "model": body.get("model") or sim.model,
        }
        if body.get("stream"):

            async def gen():
                for ch in _chunks(msg, finish):
                    yield f"data: {json.dumps({**base, 'object': 'chat.completion.chunk', **ch})}\n\n"
                last = {
                    **base,
                    "object": "chat.completion.chunk",
                    "choices": [],
                    "usage": usage,
                    "timings": timings,
                }
                yield f"data: {json.dumps(last)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream")
        return {
            **base,
            "object": "chat.completion",
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": usage,
            "timings": timings,
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

    @app.post("/_sim/loading")
    async def set_loading(request: Request):
        sim.loading = bool((await request.json()).get("loading"))
        return {"loading": sim.loading}

    return app
