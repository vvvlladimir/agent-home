"""Fake Jetson metrics/control endpoint (jetson/metrics/server.py contract)."""

from __future__ import annotations

import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sim.fake_llm import LLMSim


class JetsonSim:
    def __init__(self, llm: LLMSim | None = None):
        self.llm = llm
        self.token = "jetson-sim"
        self.profiles = ["qwen35-4b-balanced", "qwen35-2b-fast", "qwen35-9b-experimental"]
        self.active_profile = self.profiles[0]
        self.swap_seconds = 0.2
        self.ram_used_mb, self.ram_total_mb = 4600.0, 7620.0
        self.swap_used_mb, self.swap_total_mb = 0.0, 3810.0
        self.cpu_percent, self.gpu_percent = 12.0, 3.0
        self.temp_cpu_c, self.temp_gpu_c, self.temp_tj_c = 52.0, 51.0, 53.0
        self.power_mw = 9800.0
        self.throttled = False
        self.profile_calls: list[str] = []

    def metrics(self) -> dict:
        health = "loading" if self.llm and self.llm.loading else "ok"
        return {
            "schema_version": 1,
            "ts": time.time(),
            "source": "sim",
            "hostname": "jetson-sim",
            "module": "NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super",
            "l4t_version": "36.4.4",
            "jetpack_major": 6,
            "power_mode": "MAXN_SUPER",
            "ram_used_mb": self.ram_used_mb,
            "ram_total_mb": self.ram_total_mb,
            "swap_used_mb": self.swap_used_mb,
            "swap_total_mb": self.swap_total_mb,
            "cpu_percent": self.cpu_percent,
            "gpu_percent": self.gpu_percent,
            "temp_cpu_c": self.temp_cpu_c,
            "temp_gpu_c": self.temp_gpu_c,
            "temp_tj_c": self.temp_tj_c,
            "power_mw": self.power_mw,
            "throttled": self.throttled,
            "llama": {"active_profile": self.active_profile, "service_state": "active", "health": health},
            "raw": None,
        }


def create_app(sim: JetsonSim | None = None) -> FastAPI:
    sim = sim or JetsonSim()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sim = sim

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/metrics.json")
    async def metrics():
        return sim.metrics()

    @app.post("/profile/{name}")
    async def profile(name: str, request: Request):
        if request.headers.get("authorization") != f"Bearer {sim.token}":
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        if name not in sim.profiles:
            return JSONResponse({"ok": False, "error": "unknown profile"}, status_code=404)
        sim.active_profile = name
        sim.profile_calls.append(name)
        if sim.llm:
            sim.llm.loading = True
            llm = sim.llm
            t = threading.Timer(sim.swap_seconds, lambda: setattr(llm, "loading", False))
            t.daemon = True
            t.start()
        return {"ok": True, "active_profile": name}

    return app
