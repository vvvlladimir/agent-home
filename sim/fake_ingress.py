"""Fake Supervisor ingress proxy: only /api/hassio_ingress/<token>/... is reachable, as in real HA."""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import websockets
from fastapi import FastAPI, WebSocket
from fastapi.requests import Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse

HOP = {"host", "connection", "content-length", "transfer-encoding", "keep-alive", "upgrade"}
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def create_app(upstream: str, token: str = "simtoken") -> FastAPI:
    upstream = upstream.rstrip("/")
    prefix = f"/api/hassio_ingress/{token}"
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/")
    async def root():
        return RedirectResponse(f"{prefix}/", status_code=302)

    @app.api_route("/api/hassio_ingress/{tok}/{path:path}", methods=METHODS)
    async def proxy(tok: str, path: str, request: Request):
        if tok != token:
            return Response("Not Found", status_code=404)
        url = f"{upstream}/{path}" + (f"?{request.url.query}" if request.url.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        headers["X-Ingress-Path"] = prefix
        client = httpx.AsyncClient(timeout=None)
        req = client.build_request(request.method, url, headers=headers, content=await request.body())
        try:
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as e:
            await client.aclose()
            return Response(f"Bad Gateway: {e}", status_code=502)

        async def body():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        out = {k: v for k, v in resp.headers.items() if k.lower() not in HOP}
        return StreamingResponse(body(), status_code=resp.status_code, headers=out)

    @app.websocket("/api/hassio_ingress/{tok}/{path:path}")
    async def ws_proxy(ws: WebSocket, tok: str, path: str):
        if tok != token:
            await ws.close(code=1008)
            return
        q = f"?{ws.url.query}" if ws.url.query else ""
        target = upstream.replace("http", "ws", 1) + f"/{path}{q}"
        subs = ws.headers.get("sec-websocket-protocol")
        try:
            async with websockets.connect(
                target,
                additional_headers={"X-Ingress-Path": prefix},
                subprotocols=[s.strip() for s in subs.split(",")] if subs else None,
            ) as up:
                await ws.accept(subprotocol=up.subprotocol)

                async def down():
                    async for m in up:
                        await (ws.send_text(m) if isinstance(m, str) else ws.send_bytes(m))

                async def upl():
                    while True:
                        m = await ws.receive()
                        if m["type"] == "websocket.disconnect":
                            return
                        await up.send(m["text"] if m.get("text") is not None else m["bytes"])

                tasks = [asyncio.create_task(down()), asyncio.create_task(upl())]
                _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
        except Exception:
            with contextlib.suppress(Exception):
                await ws.close(code=1011)

    return app
