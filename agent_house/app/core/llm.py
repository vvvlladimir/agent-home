"""LLM manager: the only way the add-on talks to llama-server on the Jetson.

One model, one slot (`-np 1`), so requests are serialised here instead of piling up on the Jetson:

- priority queue: interactive (a human is waiting) > agent (room wakes) > background (benchmarks);
- coalescing: a queued request with the same `coalesce_key` is replaced by the newer one (ten
  motion events while the model is busy become one wake with the latest context);
- stale drop: a request that waited longer than its `max_age_s` is dropped, not answered late;
- health: `/health` probe → unconfigured / ok / warming / busy / unreachable / degraded;
  while the model loads (HTTP 503, or a profile swap in progress) requests wait instead of failing;
- retry with backoff for connection errors, 5xx and unparsable responses; never for 4xx or timeouts;
- metrics: rolling medians of decode / prefill tokens per second and time to first token.

Callers get `LLMDropped` (superseded, stale, queue full) or `LLMUnavailable` (the Jetson is off)
and decide what that means; the orchestrator's cloud fallback (M4) hooks in there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum

import httpx

from ..config import RuntimeSettings, Settings
from . import profiles
from .toolcalls import ToolCall, extract_tool_calls, visible_text
from .wire import StreamAccumulator, parse_completion, read_timings

log = logging.getLogger(__name__)

SWAP_TIMEOUT_S = 900.0  # a profile swap may download a multi-GB GGUF on first use
DEGRADED_AFTER = 3  # consecutive failed requests while /health still says ok


class Priority(IntEnum):
    INTERACTIVE = 0
    AGENT = 1
    BACKGROUND = 2


class LLMError(Exception):
    """The request failed and retrying did not help."""


class LLMUnavailable(LLMError):
    """No usable local model right now (not configured, unreachable, still loading at the deadline)."""


class LLMDropped(LLMError):
    """The request was never sent. `reason`: coalesced | stale | queue_full | shutdown."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"request dropped: {reason}")
        self.reason = reason


class _Retry(Exception):
    def __init__(self, detail: str, loading: bool = False) -> None:
        super().__init__(detail)
        self.loading = loading


@dataclass
class LLMResult:
    message: dict
    text: str
    tool_calls: list[ToolCall]
    finish_reason: str | None
    timings: dict
    ttft_ms: float | None
    total_ms: float
    queued_ms: float
    attempts: int
    model: str | None

    def as_chat_result(self) -> dict:
        return {
            "message": self.message,
            "finish_reason": self.finish_reason,
            "timings": self.timings,
            "ttft_ms": self.ttft_ms,
            "total_ms": self.total_ms,
            "error": None,
        }


@dataclass
class _Job:
    body: dict
    priority: Priority
    seq: int
    key: str | None
    max_age_s: float | None
    timeout_s: float
    future: asyncio.Future
    enqueued: float = field(default_factory=time.monotonic)

    @property
    def order(self) -> tuple[int, int]:
        return (int(self.priority), self.seq)


class LLMManager:
    def __init__(
        self,
        settings: Settings,
        runtime: RuntimeSettings | None = None,
        monitor=None,
        *,
        health_interval: float = 10.0,
        retry_backoff: float = 0.5,
    ) -> None:
        self.settings = settings
        self.runtime = runtime or RuntimeSettings()
        self.monitor = monitor
        self.health_interval = health_interval
        self.retry_backoff = retry_backoff
        self.base = settings.jetson_llm_url.rstrip("/")
        self.model: str | None = None
        self.last_error: str | None = None
        self.requests_total = self.dropped_total = self.errors_total = self.retries_total = 0
        self._health = "unreachable" if self.base else "unconfigured"
        self._swap_until = 0.0
        self._swap_profile: str | None = None
        self._failures = 0
        self._queue: list[_Job] = []
        self._seq = 0
        self._wake = asyncio.Event()
        self._in_flight: _Job | None = None
        self._current: asyncio.Task | None = None
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self._decode: deque[float] = deque(maxlen=20)
        self._prefill: deque[float] = deque(maxlen=20)
        self._ttft: deque[float] = deque(maxlen=20)
        headers = {"Authorization": f"Bearer {settings.jetson_token}"} if settings.jetson_token else {}
        self._http = httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(None, connect=5.0))

    # --- lifecycle ---------------------------------------------------------------------
    async def start(self) -> None:
        self._stopping = False
        self._tasks = [
            asyncio.create_task(self._health_loop(), name="llm-health"),
            asyncio.create_task(self._worker(), name="llm-worker"),
        ]

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for job in self._queue:
            if not job.future.done():
                job.future.set_exception(LLMDropped("shutdown"))
        self._queue.clear()
        await self._http.aclose()

    # --- state -------------------------------------------------------------------------
    @property
    def state(self) -> str:
        return "busy" if self._health == "ok" and self._in_flight is not None else self._health

    @property
    def usable(self) -> bool:
        return self._health == "ok"

    @property
    def swapping(self) -> bool:
        return time.monotonic() < self._swap_until

    @property
    def active_profile(self) -> str | None:
        return self._swap_profile or (self.monitor.active_profile if self.monitor else None)

    def status(self) -> dict:
        def median(values: deque[float]) -> float | None:
            return round(statistics.median(values), 1) if values else None

        return {
            "state": self.state,
            "url": self.settings.masked()["jetson_llm_url"] or None,
            "active_profile": self.active_profile,
            "model": self.model,
            "queue_depth": len(self._queue),
            "in_flight": self._in_flight.priority.name.lower() if self._in_flight else None,
            "last_error": self.last_error,
            "decode_tps": median(self._decode),
            "prefill_tps": median(self._prefill),
            "ttft_ms": median(self._ttft),
            "requests_total": self.requests_total,
            "dropped_total": self.dropped_total,
            "errors_total": self.errors_total,
            "retries_total": self.retries_total,
        }

    def begin_swap(self, profile: str, timeout: float = SWAP_TIMEOUT_S) -> None:
        """A profile swap was requested: report `warming` until the new model answers /health."""
        self._swap_until = time.monotonic() + timeout
        self._swap_profile = profile
        self._health, self.model, self.last_error = "warming", None, f"switching to {profile}"

    def end_swap(self) -> None:
        self._swap_until, self._swap_profile = 0.0, None

    async def probe(self) -> str:
        if not self.base:
            return self._health
        try:
            response = await self._http.get(f"{self.base}/health", timeout=4.0)
        except httpx.HTTPError as err:
            if self.swapping:
                self._health = "warming"  # the unit is restarting; connection refused is expected
            else:
                self._health, self.model = "unreachable", None
                self.last_error = f"{type(err).__name__}: {err}"
            return self._health
        if response.status_code == 200:
            if self._health != "ok":
                log.info("llama-server is healthy at %s", self.settings.masked()["jetson_llm_url"])
            self.end_swap()
            self._health = "degraded" if self._failures >= DEGRADED_AFTER else "ok"
            if self._health == "ok":
                self.last_error = None
            if self.model is None:
                await self._read_model()
        elif response.status_code == 503:  # llama-server answers 503 while the model loads
            self._health, self.last_error = "warming", "model is loading"
        else:
            self._health, self.last_error = "degraded", f"/health returned HTTP {response.status_code}"
        return self._health

    async def _read_model(self) -> None:
        try:
            data = (await self._http.get(f"{self.base}/v1/models", timeout=4.0)).json()
            self.model = (data.get("data") or [{}])[0].get("id")
        except (httpx.HTTPError, ValueError, AttributeError, IndexError, TypeError):
            pass

    async def _health_loop(self) -> None:
        while True:
            try:
                await self.probe()
            except Exception:  # noqa: BLE001 - the probe must survive anything
                log.exception("llm health probe failed")
            await asyncio.sleep(
                min(1.0, self.health_interval) if self._health == "warming" else self.health_interval
            )

    # --- submitting --------------------------------------------------------------------
    def request_body(
        self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int | None = None
    ) -> dict:
        """Messages + the active profile's sampling (+ the owner's presence-penalty override)."""
        profile = profiles.get_profile(self.active_profile) or profiles.get_profile(profiles.DEFAULT_PROFILE)
        body = {
            "messages": messages,
            "max_tokens": max_tokens or profile["max_output_tokens"],
            **profiles.sampling(profile, self.runtime.llm_presence_penalty),
        }
        if tools:
            body["tools"] = tools
        return body

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        max_tokens: int | None = None,
        **options,
    ) -> LLMResult:
        return await self.submit(self.request_body(messages, tools, max_tokens), **options)

    async def submit(
        self,
        body: dict,
        *,
        priority: Priority = Priority.AGENT,
        coalesce_key: str | None = None,
        max_age_s: float | None = None,
        timeout_s: float | None = None,
    ) -> LLMResult:
        """Queue one chat completion and wait for it. Raises LLMDropped / LLMUnavailable / LLMError."""
        if self._health == "unconfigured":
            raise LLMUnavailable("no local model configured (jetson_llm_url is empty)")
        if self._stopping:
            raise LLMDropped("shutdown")
        if max_age_s is None and priority is Priority.AGENT:
            max_age_s = self.runtime.llm_stale_after_s
        self._seq += 1
        job = _Job(
            body=body,
            priority=priority,
            seq=self._seq,
            key=coalesce_key,
            max_age_s=max_age_s,
            timeout_s=timeout_s or self.runtime.llm_timeout_s,
            future=asyncio.get_running_loop().create_future(),
        )
        if coalesce_key is not None:
            for old in self._queue:
                if old.key == coalesce_key:
                    # the newer request takes the older one's place in the line
                    job.seq, job.priority = old.seq, min(old.priority, job.priority)
                    self._drop(old, "coalesced")
                    break
        if len(self._queue) >= self.runtime.llm_max_queue:
            worst = max(self._queue, key=lambda j: j.order)
            if worst.order <= job.order:
                self.dropped_total += 1
                raise LLMDropped("queue_full")
            self._drop(worst, "queue_full")
        self._queue.append(job)
        self._wake.set()
        try:
            return await job.future
        except asyncio.CancelledError:
            if job in self._queue:
                self._queue.remove(job)
            elif self._in_flight is job and self._current is not None:
                self._current.cancel()  # nobody is waiting for the answer: free the slot
            raise

    def _drop(self, job: _Job, reason: str) -> None:
        if job in self._queue:
            self._queue.remove(job)
        self.dropped_total += 1
        if not job.future.done():
            job.future.set_exception(LLMDropped(reason))

    # --- worker ------------------------------------------------------------------------
    async def _worker(self) -> None:
        while True:
            while not self._queue:
                self._wake.clear()
                await self._wake.wait()
            job = min(self._queue, key=lambda j: j.order)
            self._queue.remove(job)
            if job.future.done():
                continue
            if self._too_old(job):
                self._drop(job, "stale")
                continue
            self._in_flight = job
            self._current = asyncio.create_task(self._run(job))
            try:
                result = await self._current
                if not job.future.done():
                    job.future.set_result(result)
            except asyncio.CancelledError:
                if self._stopping or not self._current.cancelled():
                    self._current.cancel()
                    raise
            except LLMDropped as err:
                self._drop(job, err.reason)
            except LLMError as err:
                self.errors_total += 1
                self.last_error = str(err)
                if not job.future.done():
                    job.future.set_exception(err)
            except Exception as err:  # noqa: BLE001 - one bad request must not kill the worker
                log.exception("llm request crashed")
                self.errors_total += 1
                if not job.future.done():
                    job.future.set_exception(LLMError(f"{type(err).__name__}: {err}"))
            finally:
                self._in_flight = self._current = None

    @staticmethod
    def _too_old(job: _Job) -> bool:
        return job.max_age_s is not None and time.monotonic() - job.enqueued > job.max_age_s

    async def _wait_ready(self, job: _Job, deadline: float) -> None:
        """Fail fast when the Jetson is off; wait (bounded) while the model is loading."""
        if self._health in ("unreachable", "warming"):
            await self.probe()
        while self._health == "warming":
            if self._too_old(job):
                raise LLMDropped("stale")
            if time.monotonic() >= deadline:
                raise LLMUnavailable("the model is still loading")
            await asyncio.sleep(min(0.5, self.health_interval))
            await self.probe()
        if self._health == "unreachable":
            raise LLMUnavailable(self.last_error or "llama-server is unreachable")

    async def _run(self, job: _Job) -> LLMResult:
        started = time.monotonic()
        deadline = started + job.timeout_s
        queued_ms = (started - job.enqueued) * 1000
        attempts = 0
        while True:
            await self._wait_ready(job, deadline)
            attempts += 1
            self.requests_total += 1
            try:
                async with asyncio.timeout(max(0.1, deadline - time.monotonic())):
                    result = await self._once(job, queued_ms, attempts)
                self._failures = 0
                if self._health == "degraded":
                    self._health = "ok"
                return result
            except TimeoutError:
                self._failures += 1
                raise LLMError(f"no complete answer within {job.timeout_s:.0f} s") from None
            except _Retry as err:
                self._failures += 1
                if err.loading:
                    self._health, self.last_error = "warming", "model is loading"
                elif self._failures >= DEGRADED_AFTER and self._health == "ok":
                    self._health = "degraded"
                if attempts > self.runtime.llm_max_retries:
                    raise LLMError(f"{err} (after {attempts} attempts)") from None
                self.retries_total += 1
                log.warning("llm request failed (%s), retry %d", err, attempts)
                if not err.loading:
                    await asyncio.sleep(self.retry_backoff * 2 ** (attempts - 1))

    async def _once(self, job: _Job, queued_ms: float, attempts: int) -> LLMResult:
        body = {**job.body, "stream": True, "stream_options": {"include_usage": True}}
        started = time.monotonic()
        ttft_ms: float | None = None
        try:
            async with self._http.stream("POST", f"{self.base}/v1/chat/completions", json=body) as response:
                if response.status_code == 503:
                    raise _Retry("HTTP 503", loading=True)
                if response.status_code >= 500:
                    raise _Retry(f"HTTP {response.status_code}")
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", "replace")[:300]
                    raise LLMError(f"llama-server rejected the request: HTTP {response.status_code} {detail}")
                if "text/event-stream" in response.headers.get("content-type", ""):
                    stream = StreamAccumulator()
                    async for line in response.aiter_lines():
                        if stream.feed_line(line) and ttft_ms is None:
                            ttft_ms = (time.monotonic() - started) * 1000
                    if stream.error:
                        raise _Retry(f"stream error: {stream.error}")
                    if not stream.chunks:
                        raise _Retry("empty stream")
                    parsed = stream.result()
                else:  # a build that ignores `stream`, or a proxy in between
                    try:
                        parsed = parse_completion(json.loads(await response.aread()))
                    except ValueError as err:
                        raise _Retry(f"unparsable response: {err}") from None
        except httpx.TransportError as err:
            await self.probe()
            raise _Retry(f"{type(err).__name__}: {err}") from None
        total_ms = (time.monotonic() - started) * 1000
        timings = read_timings(parsed["timings"], parsed["usage"])
        message = parsed["message"]
        tools = [t.get("function", {}).get("name") for t in job.body.get("tools") or []]
        for values, value in (
            (self._decode, timings["decode_tps"]),
            (self._prefill, timings["prefill_tps"]),
            (self._ttft, ttft_ms),
        ):
            if value is not None and (values is not self._decode or (timings["predicted_n"] or 0) >= 8):
                values.append(value)
        return LLMResult(
            message=message,
            text=visible_text(message),
            tool_calls=extract_tool_calls(message, parsed["finish_reason"], tools or None),
            finish_reason=parsed["finish_reason"],
            timings=timings,
            ttft_ms=round(ttft_ms, 1) if ttft_ms is not None else None,
            total_ms=round(total_ms, 1),
            queued_ms=round(queued_ms, 1),
            attempts=attempts,
            model=parsed["model"],
        )
