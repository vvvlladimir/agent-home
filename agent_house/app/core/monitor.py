"""Reachability of the Jetson: llama-server `/health` and the metrics endpoint.

M1 only reports state for the dashboard. The LLM manager (M2) builds its queue, model swap and
degradation handling on top of this probe.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from ..config import Settings

log = logging.getLogger(__name__)


class JetsonMonitor:
    def __init__(self, settings: Settings, interval: float = 10.0) -> None:
        self.settings = settings
        self.interval = interval
        self.llm_state = "unconfigured" if not settings.jetson_llm_url else "unreachable"
        self.llm_error: str | None = None
        self.llm_model: str | None = None
        self.metrics: dict | None = None
        self.metrics_seen: float | None = None
        self.metrics_error: str | None = None
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=2.0))
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="jetson-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._http.aclose()

    async def _run(self) -> None:
        while True:
            await self.probe()
            await asyncio.sleep(self.interval)

    async def probe(self) -> None:
        await asyncio.gather(self._probe_llm(), self._probe_metrics())

    async def _probe_llm(self) -> None:
        base = self.settings.jetson_llm_url.rstrip("/")
        if not base:
            return
        try:
            response = await self._http.get(f"{base}/health")
            if response.status_code == 200:
                self.llm_state, self.llm_error = "ok", None
                if self.llm_model is None:
                    await self._read_model(base)
            elif response.status_code == 503:  # llama-server answers 503 while the model loads
                self.llm_state, self.llm_error = "warming", "model is loading"
            else:
                self.llm_state, self.llm_error = "degraded", f"/health returned HTTP {response.status_code}"
        except httpx.HTTPError as err:
            self.llm_state, self.llm_error, self.llm_model = (
                "unreachable",
                f"{type(err).__name__}: {err}",
                None,
            )

    async def _read_model(self, base: str) -> None:
        try:
            data = (await self._http.get(f"{base}/v1/models")).json()
            self.llm_model = (data.get("data") or [{}])[0].get("id")
        except (httpx.HTTPError, ValueError, AttributeError, IndexError):
            pass

    async def _probe_metrics(self) -> None:
        base = self.settings.jetson_metrics_url.rstrip("/")
        if not base:
            return
        try:
            response = await self._http.get(f"{base}/metrics.json")
            response.raise_for_status()
            metrics = response.json()
            if not isinstance(metrics, dict):
                raise ValueError("metrics is not an object")
            self.metrics, self.metrics_seen, self.metrics_error = metrics, time.time(), None
        except (httpx.HTTPError, ValueError) as err:
            self.metrics_error = f"{type(err).__name__}: {err}"

    def llm_status(self) -> dict:
        active = ((self.metrics or {}).get("llama") or {}).get("active_profile")
        return {
            "state": self.llm_state,
            "url": self.settings.jetson_llm_url or None,
            "active_profile": active,
            "model": self.llm_model,
            "queue_depth": 0,
            "in_flight": None,
            "last_error": self.llm_error,
            "decode_tps": None,
            "prefill_tps": None,
            "ttft_ms": None,
            "requests_total": 0,
            "dropped_total": 0,
        }

    def jetson_status(self) -> dict:
        fresh = self.metrics_seen is not None and time.time() - self.metrics_seen < 3 * self.interval
        return {
            "reachable": fresh,
            "configured": bool(self.settings.jetson_metrics_url),
            "last_seen": self.metrics_seen,
            "metrics": self.metrics if fresh else None,
            "last_error": self.metrics_error,
        }
