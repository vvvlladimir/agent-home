"""The ports the add-on talks to in `make sim`, switchable from the sim panel at runtime.

    :8080  the learner (D38)                   → the real jetson/learner in this process | the real
                                                 Jetson by IP | off (down: predict.py takes over)
    :8081  Claude API for the orchestrator     → fake | api.anthropic.com | claude -p
    :8082  OpenAI API for the orchestrator     → fake | api.openai.com

The add-on keeps its simulator settings (`sk-ant-sim`, `jetson-sim`, ...); a gateway swaps in the
real key from the panel only when it forwards upstream, so keys never reach the add-on, its
settings, its traces or the browser. Every request lands in `RequestLog` (no bodies, no keys).
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


class RequestLog:
    def __init__(self, size: int = 300):
        self.rows: deque[dict] = deque(maxlen=size)
        self.total = 0  # every request since boot; the rows keep only the last `size`
        self.failed: deque[dict] = deque(maxlen=2000)  # answers >= 400, kept longer for long runs

    def add(self, **row: Any) -> None:
        entry = {"ts": time.time(), "seq": self.total, **row}
        self.total += 1
        self.rows.append(entry)
        if int(row.get("status") or 0) >= 400:
            self.failed.append(entry)

    def list(self) -> list[dict]:
        return list(reversed(self.rows))


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


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
        return _error(502, f"sim gateway: upstream {base} unreachable: {type(err).__name__}")

    async def stream():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    keep = {k: v for k, v in resp.headers.items() if k.lower() not in HOP | {"content-encoding"}}
    return StreamingResponse(stream(), status_code=resp.status_code, headers=keep)


# ---- the gateways ---------------------------------------------------------------------------------
class Gateways:
    def __init__(self, cfg: SimConfig, internal: dict[str, str], upstream: dict[str, str] | None = None):
        # upstream: the real provider URLs; tests point them at the fakes
        self.upstream = {"anthropic": ANTHROPIC_URL, "openai": OPENAI_URL, **(upstream or {})}
        self.cfg, self.internal = cfg, internal  # internal: name → in-process server URL
        self.log = RequestLog()
        self._http: dict[int, httpx.AsyncClient] = {}  # per event loop: each gateway has its own thread

    def http(self) -> httpx.AsyncClient:
        loop = id(asyncio.get_running_loop())
        if loop not in self._http:
            self._http[loop] = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0))
        return self._http[loop]

    # -- :8080 the learner
    def learner_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.api_route("/{path:path}", methods=["GET", "POST", "PUT"])
        async def any_path(path: str, request: Request):
            backend, started = self.cfg.jetson.learner, time.monotonic()
            fault = self.cfg.faults["learner"]
            if fault.latency_s:
                await asyncio.sleep(fault.latency_s)
            if backend == "off":
                return _error(503, "sim panel: the learner is off")
            if status := fault.take():
                self.log.add(gateway="learner", backend=backend, path=path, status=status, ms=0,
                             note="injected fault")  # fmt: skip
                return _error(status, "sim panel: injected failure")
            if backend == "jetson":
                base = self.cfg.jetson.learner_url()
                if not base:
                    return _error(503, "sim panel: set the Jetson address first")
                token = self.cfg.secret("jetson_token")
                headers = {"Authorization": f"Bearer {token}"} if token else None
            else:  # the add-on's own token reaches the in-process learner unchanged
                given = request.headers.get("authorization")
                base, headers = self.internal["learner"], {"Authorization": given} if given else None
            resp = await _forward(self.http(), request, base, path, headers)
            if path != "status":
                self.log.add(gateway="learner", backend=backend, path=path, status=resp.status_code,
                             ms=round((time.monotonic() - started) * 1000))  # fmt: skip
            return resp

        return app

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
