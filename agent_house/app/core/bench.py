"""The GUI benchmark: the same plan as `jetson/bench/bench.py`, sent through the LLM manager at
background priority so it never delays a room agent. One run at a time; results are stored."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime

from ..store.db import Store
from ..store.feed import Feed
from . import benchkit, profiles
from .llm import LLMDropped, LLMError, LLMManager, Priority
from .monitor import JetsonMonitor

log = logging.getLogger(__name__)


class BenchBusy(Exception):
    pass


class BenchRunner:
    def __init__(self, llm: LLMManager, monitor: JetsonMonitor, store: Store, feed: Feed) -> None:
        self.llm, self.monitor, self.store, self.feed = llm, monitor, store, feed
        self.progress: dict | None = None
        self._task: asyncio.Task | None = None
        # a run that was cut short by a restart can never finish
        store.execute("UPDATE benchmarks SET status='error' WHERE status='running'")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, quick: bool = False) -> str:
        if self.running:
            raise BenchBusy("a benchmark is already running")
        if not self.llm.usable:
            raise BenchBusy(f"the local model is not ready (state: {self.llm.state})")
        name = self.llm.active_profile
        if self.llm.runtime.llm_presence_penalty is not None and profiles.get_profile(name):
            name = f"{name}@pp{self.llm.runtime.llm_presence_penalty:g}"
        profile = profiles.get_profile(name) or {
            **profiles.get_profile(profiles.DEFAULT_PROFILE),
            "name": name or "unknown",
            "model": self.llm.model,
        }
        run_id = uuid.uuid4().hex[:12]
        created = datetime.now(UTC).isoformat(timespec="seconds")
        self.store.execute(
            "INSERT INTO benchmarks (id, created, profile, source, status) "
            "VALUES (?, ?, ?, 'addon', 'running')",
            (run_id, created, profile["name"]),
        )
        self.progress = {"id": run_id, "done": 0, "total": 0, "label": "starting"}
        self._task = asyncio.create_task(self._run(run_id, profile, quick), name="benchmark")
        self.feed.add("bench", f"Benchmark started for {profile['name']}" + (" (quick)" if quick else ""))
        return run_id

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _chat(self, body: dict) -> dict:
        try:
            result = await self.llm.submit(body, priority=Priority.BACKGROUND)
            return result.as_chat_result()
        except (LLMError, LLMDropped) as err:
            return {"error": str(err), "total_ms": 0.0}

    async def _sample(self, tracker: benchkit.ResourceTracker) -> None:
        while True:
            tracker.add(await self.monitor.probe())
            await asyncio.sleep(2.0)

    async def _run(self, run_id: str, profile: dict, quick: bool) -> None:
        def on_progress(done: int, total: int, label: str) -> None:
            self.progress = {"id": run_id, "done": done, "total": total, "label": label}

        tracker = benchkit.ResourceTracker()
        sampler = asyncio.create_task(self._sample(tracker))
        status, report = "error", None
        try:
            doc = await asyncio.to_thread(benchkit.load_eval)
            plan = benchkit.bench_plan(
                doc,
                profiles.sampling(profile),
                quick=quick,
                max_tokens=profile["max_output_tokens"],
                progress=on_progress,
            )
            try:
                body = next(plan)
                while True:
                    body = plan.send(await self._chat(body))
            except StopIteration as stop:
                parts = stop.value
            report = benchkit.build_report(
                source="addon",
                profile=profile,
                server={
                    "url": self.llm.settings.masked()["jetson_llm_url"],
                    "model_reported": self.llm.model,
                    "cold_load_s": None,
                },
                parts=parts,
                host=benchkit.host_from_metrics(self.monitor.metrics),
                resources=tracker.summary() if tracker.samples else {},
            )
            status = "done"
            tool = report["toolcall"]
            self.feed.add(
                "bench",
                f"Benchmark finished for {profile['name']}: tool calls {tool['passed']}/{tool['total']}, "
                f"decode {report['speed']['decode_tps']} tok/s",
                data={"id": run_id},
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - report the failure instead of losing the task
            log.exception("benchmark failed")
            self.feed.add("bench", f"Benchmark failed: {type(err).__name__}: {err}", level="error")
        finally:
            sampler.cancel()
            self.progress = None
            self.store.execute(
                "UPDATE benchmarks SET status=?, report=? WHERE id=?",
                (status, json.dumps(report) if report else None, run_id),
            )

    def list(self, limit: int = 50) -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM benchmarks ORDER BY created DESC, rowid DESC LIMIT ?", (limit,)
        )
        for row in rows:
            row["report"] = json.loads(row["report"]) if row["report"] else None
            if self.progress and self.progress["id"] == row["id"]:
                row["progress"] = self.progress
        return rows
