import json
import time
from pathlib import Path

import httpx
import jsonschema
import pytest
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import StreamingResponse

from sim.fake_ingress import create_app as ingress_app
from sim.fake_jetson import JetsonSim
from sim.fake_jetson import create_app as jetson_app
from sim.fake_llm import LLMSim
from sim.server import ServerThread

SCHEMA = json.loads((Path(__file__).parents[2] / "contracts/schemas/jetson_metrics.schema.json").read_text())


def test_jetson_metrics_and_profile():
    llm = LLMSim()
    sim = JetsonSim(llm)
    with ServerThread(jetson_app(sim)) as srv:
        assert httpx.get(f"{srv.url}/healthz").json() == {"ok": True}
        m = httpx.get(f"{srv.url}/metrics.json").json()
        jsonschema.validate(m, SCHEMA)
        assert m["source"] == "sim" and m["ram_used_mb"] == 4600 and m["power_mode"] == "MAXN_SUPER"
        sim.ram_used_mb, sim.throttled = 7000.0, True
        m = httpx.get(f"{srv.url}/metrics.json").json()
        jsonschema.validate(m, SCHEMA)
        assert m["ram_used_mb"] == 7000 and m["throttled"] is True
        auth = {"Authorization": "Bearer jetson-sim"}
        assert httpx.post(f"{srv.url}/profile/qwen35-2b-fast").status_code == 401
        assert httpx.post(f"{srv.url}/profile/nope", headers=auth).status_code == 404
        r = httpx.post(f"{srv.url}/profile/qwen35-2b-fast", headers=auth)
        assert r.json() == {"ok": True, "active_profile": "qwen35-2b-fast"}
        assert llm.loading
        assert httpx.get(f"{srv.url}/metrics.json").json()["llama"]["active_profile"] == "qwen35-2b-fast"
        time.sleep(0.5)
        assert not llm.loading


@pytest.fixture
def ingress():
    up = FastAPI()

    @up.api_route("/echo", methods=["GET", "POST"])
    async def echo(request: Request):
        return {
            "ingress": request.headers.get("x-ingress-path"),
            "q": request.url.query,
            "method": request.method,
            "body": (await request.body()).decode(),
        }

    @up.get("/sse")
    async def sse():
        async def gen():
            for i in range(3):
                yield f"data: {i}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @up.websocket("/ws")
    async def ws(w: WebSocket):
        await w.accept()
        await w.send_text(w.headers.get("x-ingress-path", ""))
        await w.send_text(await w.receive_text() + "!")
        await w.close()

    with ServerThread(up) as u, ServerThread(ingress_app(u.url, "tok")) as ing:
        ing.up = u
        yield ing


def test_ingress_proxy(ingress):
    p = f"{ingress.url}/api/hassio_ingress/tok"
    r = httpx.post(f"{p}/echo?a=1", content="hello")
    assert r.json() == {"ingress": "/api/hassio_ingress/tok", "q": "a=1", "method": "POST", "body": "hello"}
    assert httpx.get(f"{p}/sse").text == "data: 0\n\ndata: 1\n\ndata: 2\n\n"
    assert "text/event-stream" in httpx.get(f"{p}/sse").headers["content-type"]
    r = httpx.get(ingress.url + "/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/api/hassio_ingress/tok/"


def test_ingress_404s(ingress):
    assert httpx.get(f"{ingress.url}/echo").status_code == 404
    assert httpx.get(f"{ingress.url}/api/other").status_code == 404
    assert httpx.get(f"{ingress.url}/api/hassio_ingress/wrong/echo").status_code == 404


async def test_ingress_websocket(ingress):
    async with websockets.connect(f"{ingress.ws_url}/api/hassio_ingress/tok/ws") as ws:
        assert await ws.recv() == "/api/hassio_ingress/tok"
        await ws.send("hi")
        assert await ws.recv() == "hi!"
