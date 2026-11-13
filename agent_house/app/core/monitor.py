"""The Jetson's metrics/control endpoint (`jetson/metrics/server.py`): RAM, temperature, power mode,
active profile, and the model-profile switch. llama-server itself is the LLM manager's business.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from ..config import Settings

log = logging.getLogger(__name__)


class JetsonError(Exception):
    """The control endpoint refused or could not be reached."""


class JetsonMonitor:
    def __init__(self, settings: Settings, interval: float = 10.0) -> None:
        self.settings = settings
        self.interval = interval
        self.base = settings.jetson_metrics_url.rstrip("/")
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

    async def probe(self) -> dict | None:
        if not self.base:
            return None
        try:
            response = await self._http.get(f"{self.base}/metrics.json")
            response.raise_for_status()
            metrics = response.json()
            if not isinstance(metrics, dict):
                raise ValueError("metrics is not an object")
            self.metrics, self.metrics_seen, self.metrics_error = metrics, time.time(), None
            return metrics
        except (httpx.HTTPError, ValueError) as err:
            self.metrics_error = f"{type(err).__name__}: {err}"
            return None

    @property
    def fresh(self) -> bool:
        return self.metrics_seen is not None and time.time() - self.metrics_seen < 3 * self.interval

    @property
    def active_profile(self) -> str | None:
        llama = (self.metrics or {}).get("llama")
        return llama.get("active_profile") if self.fresh and isinstance(llama, dict) else None

    @property
    def can_switch(self) -> bool:
        return bool(self.base and self.settings.jetson_token)

    async def switch_profile(self, name: str) -> None:
        """Ask the Jetson to restart llama-server with another profile. Raises JetsonError."""
        headers = {"Authorization": f"Bearer {self.settings.jetson_token}"}
        try:
            response = await self._http.post(f"{self.base}/profile/{name}", headers=headers, timeout=90.0)
        except httpx.HTTPError as err:
            raise JetsonError(f"{type(err).__name__}: {err}") from None
        if response.status_code != 200:
            try:
                detail = response.json().get("error")
            except (ValueError, AttributeError):
                detail = None
            raise JetsonError(f"HTTP {response.status_code}: {detail or response.text[:200]}")
        await self.probe()

    def jetson_status(self) -> dict:
        return {
            "reachable": self.fresh,
            "configured": bool(self.base),
            "last_seen": self.metrics_seen,
            "metrics": self.metrics if self.fresh else None,
            "last_error": self.metrics_error,
        }
