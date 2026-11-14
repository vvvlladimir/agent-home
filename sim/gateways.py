"""The ports the add-on talks to in `make sim`, switchable from the sim panel at runtime.

    :8080  room-agent LLM (llama-server API)  → fake | real Jetson | OpenAI | Claude | OpenAI-compatible
                                                | claude -p (sim/claude_bridge.py, no key)
    :8081  Claude API for the orchestrator    → fake | api.anthropic.com | claude -p
    :8082  OpenAI API for the orchestrator    → fake | api.openai.com
    :9100  Jetson metrics + profile switch    → fake | real Jetson

The add-on keeps its simulator settings (`sk-ant-sim`, `jetson-sim`, ...); a gateway swaps in the
real key from the panel only when it forwards upstream, so keys never reach the add-on, its
settings, its traces or the browser. Every request lands in `RequestLog` (no bodies, no keys).

Cloud backends for room agents answer in llama-server's shape: OpenAI chat completions with
`timings`, streamed as SSE when asked. With `pace` on they are slowed to the Jetson's measured
prefill/decode speed, serialised like `-np 1`, and rejected like llama-server when the prompt
would not fit the Jetson's context (`n_ctx`, estimated at 3.5 characters per token).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from sim import claude_bridge
from sim.fake_jetson import JetsonSim
from sim.fake_llm import _chunks
from sim.sim_config import SimConfig

HOP = {
    "host",
    "connection",
    "content-length",
    "transfer-encoding",
    "keep-alive",
    "upgrade",
    "accept-encoding",
}
AUTH = {"authorization", "x-api-key"}
ANTHROPIC_URL = "https://api.anthropic.com"
OPENAI_URL = "https://api.openai.com"
CHARS_PER_TOKEN = 3.5
LLAMA_ONLY = (
    "top_k",
    "min_p",
    "repeat_penalty",
    "cache_prompt",
    "id_slot",
    "chat_template_kwargs",
    "n_probs",
    "samplers",
    "timings_per_token",
    "stream_options",
)


class RequestLog:
    def __init__(self, size: int = 300):
        self.rows: deque[dict] = deque(maxlen=size)

    def add(self, **row: Any) -> None:
        self.rows.append({"ts": time.time(), **row})

    def list(self) -> list[dict]:
        return list(reversed(self.rows))


def _llama_error(status: int, message: str, typ: str = "server_error", **extra: Any) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": status, "message": message, "type": typ, **extra}}, status_code=status
    )


async def _forward(
    client: httpx.AsyncClient,
    request: Request,
    base: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> Response:
    """Stream a request to `base/path` and the answer back, with hop-by-hop and auth headers replaced."""
    url = f"{base.rstrip('/')}/{path.lstrip('/')}" + (f"?{request.url.query}" if request.url.query else "")
    out = {k: v for k, v in request.headers.items() if k.lower() not in HOP | AUTH}
    out.update(headers or {})
    req = client.build_request(
        request.method, url, headers=out, content=body if body is not None else await request.body()
    )
    try:
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as err:
        return _llama_error(502, f"sim gateway: upstream {base} unreachable: {type(err).__name__}")

    async def stream():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    keep = {k: v for k, v in resp.headers.items() if k.lower() not in HOP | {"content-encoding"}}
    return StreamingResponse(stream(), status_code=resp.status_code, headers=keep)


# ---- message translation for cloud room backends ------------------------------------------------
def _text(content: Any) -> str:
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def _args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def to_anthropic(body: dict) -> dict:
    """OpenAI chat completions request → Claude Messages request (no sampling: current models reject it)."""
    system, messages = [], []
    for m in body.get("messages") or []:
        role = m.get("role")
        if role in ("system", "developer"):
            system.append(_text(m.get("content")))
        elif role == "user":
            messages.append({"role": "user", "content": _text(m.get("content")) or "."})
        elif role == "assistant":
            blocks: list[dict] = []
            if t := _text(m.get("content")).strip():
                blocks.append({"type": "text", "text": t})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id") or "call",
                        "name": fn.get("name", ""),
                        "input": _args(fn.get("arguments")),
                    }
                )
            if blocks:
                messages.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            result = {
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id") or "call",
                "content": _text(m.get("content")) or "(empty)",
            }
            if messages and messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
                messages[-1]["content"].append(result)  # all results of one turn in one user message
            else:
                messages.append({"role": "user", "content": [result]})
    if not messages or messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": "(conversation start)"})
    out: dict[str, Any] = {
        "max_tokens": int(body.get("max_tokens") or 1024),
        "messages": messages,
        "output_config": {"effort": "low"},
    }
    if system:
        out["system"] = "\n\n".join(system)
    tools = [t.get("function") or {} for t in body.get("tools") or []]
    if tools:
        out["tools"] = [
            {
                "name": f.get("name", ""),
                "description": f.get("description", ""),
                "input_schema": f.get("parameters") or {"type": "object", "properties": {}},
            }
            for f in tools
        ]
        if body.get("tool_choice") == "none":
            out["tool_choice"] = {"type": "none"}
    return out


def from_anthropic(resp: Any) -> tuple[dict, str, int, int]:
    text, calls = [], []
    for block in resp.content:
        if block.type == "text":
            text.append(block.text)
        elif block.type == "tool_use":
            calls.append(
                {
                    "id": block.id,
                    "type": "function",
                    "function": {"name": block.name, "arguments": json.dumps(block.input)},
                }
            )
    msg: dict[str, Any] = {"role": "assistant", "content": "".join(text) or (None if calls else "")}
    if calls:
        msg["tool_calls"] = calls
    finish = {"tool_use": "tool_calls", "max_tokens": "length"}.get(resp.stop_reason or "", "stop")
    return msg, finish, resp.usage.input_tokens, resp.usage.output_tokens


def to_responses(body: dict) -> dict:
    """OpenAI chat completions request → OpenAI Responses request (what the GPT-6 models take)."""
    instructions, items = [], []
    for m in body.get("messages") or []:
        role = m.get("role")
        if role in ("system", "developer"):
            instructions.append(_text(m.get("content")))
        elif role in ("user", "assistant"):
            if t := _text(m.get("content")):
                items.append({"role": role, "content": t})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tc.get("id") or "call",
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments") or "{}",
                    }
                )
        elif role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": m.get("tool_call_id") or "call",
                    "output": _text(m.get("content")),
                }
            )
    out: dict[str, Any] = {
        "input": items,
        "store": False,
        "max_output_tokens": max(int(body.get("max_tokens") or 0), 4096),
    }
    if instructions:
        out["instructions"] = "\n\n".join(instructions)
    tools = [t.get("function") or {} for t in body.get("tools") or []]
    if tools:
        out["tools"] = [
            {
                "type": "function",
                "name": f.get("name", ""),
                "description": f.get("description", ""),
                "parameters": f.get("parameters") or {"type": "object", "properties": {}},
            }
            for f in tools
        ]
    return out


def from_responses(resp: Any) -> tuple[dict, str, int, int]:
    text, calls = [], []
    for item in resp.output:
        if item.type == "function_call":
            calls.append(
                {
                    "id": item.call_id,
                    "type": "function",
                    "function": {"name": item.name, "arguments": item.arguments or "{}"},
                }
            )
        elif item.type == "message":
            text += [p.text for p in item.content or [] if p.type == "output_text"]
    msg: dict[str, Any] = {"role": "assistant", "content": "".join(text) or (None if calls else "")}
    if calls:
        msg["tool_calls"] = calls
    incomplete = getattr(resp, "status", "") == "incomplete"
    finish = "tool_calls" if calls else ("length" if incomplete else "stop")
    return msg, finish, resp.usage.input_tokens, resp.usage.output_tokens


def to_compatible(body: dict) -> tuple[dict, dict]:
    """llama-server request → OpenAI-compatible server (vLLM, DeepInfra); llama-only keys go to extra_body."""
    req = {k: v for k, v in body.items() if k not in LLAMA_ONLY and k not in ("stream", "model")}
    extra = {k: body[k] for k in ("top_k", "min_p", "chat_template_kwargs") if k in body}
    return req, extra


# ---- the gateways ---------------------------------------------------------------------------------
class Gateways:
    def __init__(
        self,
        cfg: SimConfig,
        jetson: JetsonSim,
        internal: dict[str, str],
        upstream: dict[str, str] | None = None,
    ):
        # upstream: the real provider URLs; tests point them at the fakes
        self.upstream = {"anthropic": ANTHROPIC_URL, "openai": OPENAI_URL, **(upstream or {})}
        self.cfg, self.jetson, self.internal = cfg, jetson, internal  # internal: name → fake server URL
        self.log = RequestLog()
        self._slot = asyncio.Lock()
        self._http: dict[int, httpx.AsyncClient] = {}  # per event loop: each gateway has its own thread
        self._sdks: dict[str, tuple[tuple, Any]] = {}  # kind → ((key, base URL), client)

    def http(self) -> httpx.AsyncClient:
        loop = id(asyncio.get_running_loop())
        if loop not in self._http:
            self._http[loop] = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0))
        return self._http[loop]

    def sdk(self, kind: str, key: str, base_url: str | None = None):
        """One SDK client per provider, rebuilt when the panel changes its key or base URL."""
        ident = (key, base_url)
        if kind not in self._sdks or self._sdks[kind][0] != ident:
            if kind == "anthropic":
                from anthropic import AsyncAnthropic  # noqa: PLC0415

                client: Any = AsyncAnthropic(api_key=key, base_url=base_url, max_retries=1, timeout=120)
            else:
                from openai import AsyncOpenAI  # noqa: PLC0415

                client = AsyncOpenAI(api_key=key, base_url=base_url, max_retries=1, timeout=120)
            self._sdks[kind] = (ident, client)
        return self._sdks[kind][1]

    # -- :8080 room-agent LLM
    def llm_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
        async def any_path(path: str, request: Request):
            room, started = self.cfg.room, time.monotonic()
            backend = room.backend
            fault = self.cfg.faults["llm"]
            if path == "v1/chat/completions" and fault.latency_s:
                await asyncio.sleep(fault.latency_s)
            if path == "v1/chat/completions" and (status := fault.take()):
                self.log.add(
                    gateway="llm", backend=backend, path=path, status=status, ms=0, note="injected fault"
                )
                return _llama_error(status, "sim panel: injected failure")
            if backend in ("fake", "jetson"):
                base = self.internal["llm"] if backend == "fake" else self.cfg.jetson.llm_url()
                if not base:
                    return _llama_error(503, "sim panel: set the Jetson address first", "unavailable_error")
                token = self.cfg.secret("jetson_token")
                headers = {"Authorization": f"Bearer {token}"} if backend == "jetson" and token else None
                if backend == "fake" and request.headers.get("authorization"):
                    headers = {"Authorization": request.headers["authorization"]}
                resp = await _forward(self.http(), request, base, path, headers)
                if path == "v1/chat/completions":
                    self.log.add(
                        gateway="llm",
                        backend=backend,
                        path=path,
                        status=resp.status_code,
                        ms=round((time.monotonic() - started) * 1000),
                    )
                return resp
            # cloud backends: answer llama-server's own endpoints locally
            if path == "health":
                return {"status": "ok"}
            if path == "v1/models":
                return {
                    "object": "list",
                    "data": [
                        {"id": self.jetson.active_profile, "object": "model", "owned_by": f"sim:{backend}"}
                    ],
                }
            if path == "props":
                return {
                    "default_generation_settings": {"n_ctx": room.n_ctx},
                    "total_slots": 1,
                    "model_path": f"/sim/{backend}/{self._room_model()}",
                }
            if path == "v1/chat/completions" and request.method == "POST":
                return await self._cloud_chat(request, backend, started)
            return _llama_error(
                404, f"sim gateway: /{path} is not served by the {backend} backend", "not_found"
            )

        return app

    def _room_model(self) -> str:
        r = self.cfg.room
        return {
            "openai": r.openai_model,
            "anthropic": r.anthropic_model,
            "compatible": r.compatible_model,
            "claude": r.claude_model,
        }.get(r.backend, r.backend)

    async def _cloud_chat(self, request: Request, backend: str, started: float) -> Response:
        room = self.cfg.room
        try:
            body = await request.json()
        except ValueError:
            return _llama_error(400, "request body is not JSON", "invalid_request_error")
        estimate = int(len(json.dumps(body.get("messages") or [], ensure_ascii=False)) / CHARS_PER_TOKEN)
        estimate += int(len(json.dumps(body.get("tools") or [], ensure_ascii=False)) / CHARS_PER_TOKEN)
        if room.pace and estimate > room.n_ctx:
            self.log.add(
                gateway="llm",
                backend=backend,
                path="v1/chat/completions",
                status=400,
                ms=0,
                tokens_in=estimate,
                note="exceeds the Jetson context",
            )
            return _llama_error(
                400,
                "the request exceeds the available context size, try increasing it",
                "exceed_context_size_error",
                n_prompt_tokens=estimate,
                n_ctx=room.n_ctx,
            )
        key_name = {"openai": "openai_api_key", "anthropic": "anthropic_api_key"}.get(
            backend, "compatible_api_key"
        )
        key = "claude-cli" if backend == "claude" else self.cfg.secret(key_name)
        if not key:
            return _llama_error(401, f"sim panel: no {key_name} saved", "authentication_error")
        if room.single_slot and room.pace:
            async with self._slot:
                return await self._cloud_call(body, backend, key, started)
        return await self._cloud_call(body, backend, key, started)

    async def _cloud_call(self, body: dict, backend: str, key: str, started: float) -> Response:
        room = self.cfg.room
        model = self._room_model()
        t0 = time.monotonic()
        try:
            if backend == "claude":
                msg, finish, n_in, n_out = await asyncio.to_thread(
                    claude_bridge.chat_completion, body, room.claude_model, room.claude_effort
                )
            elif backend == "anthropic":
                resp = await self.sdk("anthropic", key, self.upstream["anthropic"]).messages.create(
                    model=model, **to_anthropic(body)
                )
                msg, finish, n_in, n_out = from_anthropic(resp)
            elif backend == "openai":
                resp = await self.sdk("openai", key, self.upstream["openai"] + "/v1").responses.create(
                    model=model, **to_responses(body)
                )
                msg, finish, n_in, n_out = from_responses(resp)
            else:
                req, extra = to_compatible(body)
                client = self.sdk("compatible", key, room.compatible_base_url)
                resp = await client.chat.completions.create(model=model, **req, extra_body=extra or None)
                choice = resp.choices[0]
                msg = choice.message.model_dump(exclude_none=True)
                msg.setdefault("content", None)
                finish = choice.finish_reason or "stop"
                n_in, n_out = resp.usage.prompt_tokens, resp.usage.completion_tokens
        except Exception as err:  # SDK errors carry a status; anything else is a 502
            status = int(getattr(err, "status_code", 0) or 502)
            self.log.add(
                gateway="llm",
                backend=backend,
                path="v1/chat/completions",
                model=model,
                status=status,
                ms=round((time.monotonic() - started) * 1000),
                note=type(err).__name__,
            )
            return _llama_error(status, f"sim gateway ({backend}): {type(err).__name__}: {str(err)[:240]}")
        upstream_s = time.monotonic() - t0
        if room.pace:
            prefill_s, decode_s = n_in / room.prefill_tps, n_out / room.decode_tps
        else:  # a cloud API reports no split; call half of it prompt processing
            prefill_s = decode_s = max(upstream_s / 2, 1e-3)
        timings = {
            "prompt_n": n_in,
            "prompt_ms": round(prefill_s * 1000, 1),
            "prompt_per_second": round(n_in / prefill_s, 1),
            "predicted_n": n_out,
            "predicted_ms": round(decode_s * 1000, 1),
            "predicted_per_second": round(n_out / decode_s, 1),
            "cache_n": 0,
        }
        usage = {"prompt_tokens": n_in, "completion_tokens": n_out, "total_tokens": n_in + n_out}
        base = {
            "id": f"chatcmpl-sim-{int(t0 * 1000)}",
            "created": int(time.time()),
            "model": self.jetson.active_profile,
        }
        self.log.add(
            gateway="llm",
            backend=backend,
            path="v1/chat/completions",
            model=model,
            status=200,
            ms=round((time.monotonic() - started) * 1000),
            tokens_in=n_in,
            tokens_out=n_out,
            upstream_ms=round(upstream_s * 1000),
            tool_calls=len(msg.get("tool_calls") or []),
        )
        first_at = t0 + (prefill_s if room.pace else 0)
        done_at = first_at + (decode_s if room.pace else 0)
        if not body.get("stream"):
            await asyncio.sleep(max(0.0, done_at - time.monotonic()))
            return JSONResponse(
                {
                    **base,
                    "object": "chat.completion",
                    "usage": usage,
                    "timings": timings,
                    "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                }
            )

        async def gen():
            chunks = _chunks(msg, finish)
            await asyncio.sleep(max(0.0, first_at - time.monotonic()))
            gap = max(0.0, done_at - time.monotonic()) / max(1, len(chunks) - 1)
            for i, ch in enumerate(chunks):
                if i:
                    await asyncio.sleep(gap)
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

    # -- :8081 / :8082 orchestrator cloud APIs
    def cloud_app(self, name: str) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
        async def any_path(path: str, request: Request):
            c = getattr(self.cfg, name)
            started = time.monotonic()
            fault = self.cfg.faults[name]
            if fault.latency_s:
                await asyncio.sleep(fault.latency_s)
            if status := fault.take():
                self.log.add(
                    gateway=name, backend=c.backend, path=path, status=status, ms=0, note="injected fault"
                )
                return self._cloud_error(name, status, "sim panel: injected failure")
            body = await request.body()
            if c.backend == "claude":
                return await self._claude_messages(name, path, body, started)
            if c.backend == "fake":
                base, headers = (
                    self.internal[name],
                    {k: v for k, v in request.headers.items() if k.lower() in AUTH},
                )
            else:
                key = self.cfg.secret(f"{name}_api_key")
                if not key:
                    return self._cloud_error(name, 401, f"sim panel: no {name}_api_key saved")
                base = self.upstream[name]
                headers = {"x-api-key": key} if name == "anthropic" else {"Authorization": f"Bearer {key}"}
            model = None
            if body and request.headers.get("content-type", "").startswith("application/json"):
                try:
                    data = json.loads(body)
                    if c.model_override and isinstance(data, dict) and "model" in data:
                        data["model"] = c.model_override
                        body = json.dumps(data).encode()
                    model = data.get("model") if isinstance(data, dict) else None
                except ValueError:
                    pass
            resp = await _forward(self.http(), request, base, path, headers, body)
            if isinstance(resp, StreamingResponse):  # orchestrator calls are not streamed: read it whole
                raw = b"".join([c if isinstance(c, bytes) else c.encode() async for c in resp.body_iterator])
            else:
                raw = bytes(resp.body)
            usage = {}
            try:
                u = json.loads(raw).get("usage") or {}
                usage = {"tokens_in": u.get("input_tokens"), "tokens_out": u.get("output_tokens")}
            except (ValueError, AttributeError):
                pass
            self.log.add(
                gateway=name,
                backend=c.backend,
                path=path,
                model=model,
                status=resp.status_code,
                ms=round((time.monotonic() - started) * 1000),
                **usage,
            )
            return Response(raw, status_code=resp.status_code, headers=dict(resp.headers))

        return app

    async def _claude_messages(self, name: str, path: str, body: bytes, started: float) -> Response:
        """The orchestrator's Messages API call answered by `claude -p` (D30)."""
        c = getattr(self.cfg, name)
        if name != "anthropic" or path.strip("/") != "v1/messages":
            return self._cloud_error(name, 404, f"sim gateway (claude): /{path} is not served")
        try:
            data = json.loads(body)
            resp = await asyncio.to_thread(claude_bridge.messages, data, c.claude_model, c.claude_effort)
        except (ValueError, claude_bridge.BridgeError) as err:
            self.log.add(gateway=name, backend="claude", path=path, model=c.claude_model, status=529,
                         ms=round((time.monotonic() - started) * 1000), note=type(err).__name__)  # fmt: skip
            return self._cloud_error(name, 529, f"sim gateway (claude): {str(err)[:240]}")
        self.log.add(
            gateway=name,
            backend="claude",
            path=path,
            model=resp["model"],
            status=200,
            ms=round((time.monotonic() - started) * 1000),
            tokens_in=resp["usage"]["input_tokens"],
            tokens_out=resp["usage"]["output_tokens"],
        )
        return JSONResponse(resp)

    @staticmethod
    def _cloud_error(name: str, status: int, message: str) -> JSONResponse:
        if name == "anthropic":
            typ = {401: "authentication_error", 429: "rate_limit_error", 529: "overloaded_error"}.get(
                status, "api_error"
            )
            return JSONResponse(
                {"type": "error", "error": {"type": typ, "message": message}}, status_code=status
            )
        return JSONResponse(
            {"error": {"message": message, "type": "sim_error", "param": None, "code": None}},
            status_code=status,
        )

    # -- :9100 Jetson metrics
    def metrics_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.api_route("/{path:path}", methods=["GET", "POST"])
        async def any_path(path: str, request: Request):
            if self.cfg.metrics_source() == "jetson":
                base = self.cfg.jetson.metrics_url()
                if not base:
                    return JSONResponse(
                        {"ok": False, "error": "sim panel: no Jetson address"}, status_code=503
                    )
                token = self.cfg.secret("jetson_token")
                return await _forward(
                    self.http(), request, base, path, {"Authorization": f"Bearer {token}"} if token else None
                )
            headers = {k: v for k, v in request.headers.items() if k.lower() == "authorization"}
            return await _forward(self.http(), request, self.internal["jetson"], path, headers)

        return app
