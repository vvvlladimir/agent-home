#!/usr/bin/env python3
"""Benchmark the running llama-server: speed, tool-call eval, resources. One report per profile.

    ./bench.py --profile qwen35-4b-balanced            # → owner-reports/bench/<date>_<profile>.json
    ./bench.py --profile qwen35-4b-balanced@pp0        # same model, presence penalty 0
    ./bench.py --quick                                 # short run to see that everything works

Python 3.10 + PyYAML (`sudo apt install python3-yaml`), nothing else. The measuring logic is the
add-on's (`agent_house/app/core/benchkit.py`), so the Jetson and the GUI benchmark agree.
Report format: contracts/schemas/bench_report.schema.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "agent_house"))

from app.core import benchkit, profiles  # noqa: E402
from app.core.wire import StreamAccumulator, parse_completion, read_timings  # noqa: E402


class Client:
    def __init__(
        self, url: str, token: str = "", timeout: float = 300.0, sample_file: Path | None = None
    ) -> None:
        self.url, self.timeout, self.sample_file = url.rstrip("/"), timeout, sample_file
        self.headers = {"Content-Type": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def get_json(self, path: str, timeout: float = 5.0):
        request = urllib.request.Request(self.url + path, headers=self.headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read())

    def healthy(self) -> bool:
        try:
            self.get_json("/health", timeout=3)
            return True
        except (OSError, ValueError):
            return False

    def wait_healthy(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.healthy():
                return True
            time.sleep(1)
        return False

    def model(self) -> str | None:
        try:
            return (self.get_json("/v1/models").get("data") or [{}])[0].get("id")
        except (OSError, ValueError, AttributeError, IndexError):
            return None

    def chat(self, body: dict) -> dict:
        """One streaming chat completion → ChatResult (see benchkit). Never raises."""
        result = self._chat_once(body)
        if result.get("loading") and self.wait_healthy(180):  # the model was still loading: once more
            result = self._chat_once(body)
        return result

    def _chat_once(self, body: dict) -> dict:
        payload = json.dumps({**body, "stream": True, "stream_options": {"include_usage": True}}).encode()
        request = urllib.request.Request(
            self.url + "/v1/chat/completions", data=payload, headers=self.headers
        )
        started = time.monotonic()
        ttft_ms = None
        raw_lines: list[str] = []
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                if "text/event-stream" in (response.headers.get("Content-Type") or ""):
                    stream = StreamAccumulator()
                    for raw in response:
                        line = raw.decode("utf-8", "replace")
                        raw_lines.append(line)
                        if stream.feed_line(line) and ttft_ms is None:
                            ttft_ms = (time.monotonic() - started) * 1000
                    if stream.error or not stream.chunks:
                        return self._failed(stream.error or "empty stream", started)
                    parsed = stream.result()
                else:
                    text = response.read().decode("utf-8", "replace")
                    raw_lines.append(text)
                    parsed = parse_completion(json.loads(text))
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "replace")[:200]
            return {**self._failed(f"HTTP {err.code} {detail}", started), "loading": err.code == 503}
        except (OSError, ValueError) as err:
            return self._failed(f"{type(err).__name__}: {err}", started)
        if self.sample_file and body.get("tools") and not self.sample_file.exists():
            # the raw wire format of one tool-call response, to check the field names we rely on
            self.sample_file.write_text("".join(raw_lines), encoding="utf-8")
        return {
            "message": parsed["message"],
            "finish_reason": parsed["finish_reason"],
            "timings": read_timings(parsed["timings"], parsed["usage"]),
            "ttft_ms": ttft_ms,
            "total_ms": (time.monotonic() - started) * 1000,
            "error": None,
        }

    @staticmethod
    def _failed(error: str, started: float) -> dict:
        return {"error": error, "total_ms": (time.monotonic() - started) * 1000}


class MetricsPoller(threading.Thread):
    """Samples the metrics endpoint during the run for the `resources` and `host` sections."""

    def __init__(self, url: str, interval: float = 2.0) -> None:
        super().__init__(daemon=True)
        self.url, self.interval = url.rstrip("/") if url else "", interval
        self.tracker = benchkit.ResourceTracker()
        self.last: dict | None = None
        self._halt = threading.Event()

    def poll(self) -> None:
        if not self.url:
            return
        try:
            with urllib.request.urlopen(self.url + "/metrics.json", timeout=3) as response:  # noqa: S310
                self.last = json.loads(response.read())
                self.tracker.add(self.last)
        except (OSError, ValueError):
            pass

    def run(self) -> None:
        while not self._halt.wait(self.interval):
            self.poll()

    def stop(self) -> None:
        self._halt.set()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8080", help="llama-server base URL")
    ap.add_argument("--metrics-url", default="http://127.0.0.1:9100", help="metrics endpoint ('' = none)")
    ap.add_argument("--profile", default="", help="profile name; default: the active one")
    ap.add_argument("--token-file", default="/etc/agent-house/token")
    ap.add_argument("--eval", default=str(benchkit.EVAL_FILE), help="tool-call eval file")
    ap.add_argument("--out", default=str(REPO / "owner-reports" / "bench"), help="report directory")
    ap.add_argument("--quick", action="store_true", help="1 run per measurement, 12 scenarios")
    ap.add_argument("--runs", type=int, default=0, help="runs per speed measurement (default 3)")
    ap.add_argument("--soak-minutes", type=float, default=0, help="keep generating this long at the end")
    ap.add_argument("--source", default="jetson", choices=["jetson", "sim"])
    ap.add_argument("--cold-load-s", type=float, default=None, help="measured by run_all.sh")
    ap.add_argument("--llama-build", default=None, help="llama.cpp commit, recorded in the report")
    ap.add_argument("--flags", default=None, help="llama-server command line, recorded in the report")
    ap.add_argument("--save-sample", default="", help="write one raw tool-call response to this file")
    ap.add_argument("--timeout", type=float, default=300.0, help="seconds per request")
    args = ap.parse_args(argv)

    try:
        token = Path(args.token_file).read_text().strip()
    except OSError:
        token = ""
    client = Client(args.url, token, args.timeout, Path(args.save_sample) if args.save_sample else None)
    if not client.wait_healthy(10):
        print(f"llama-server is not healthy at {args.url} (is the service running?)", file=sys.stderr)
        return 2

    poller = MetricsPoller(args.metrics_url)
    poller.poll()
    active = ((poller.last or {}).get("llama") or {}).get("active_profile")
    name = args.profile or active or profiles.DEFAULT_PROFILE
    profile = profiles.get_profile(name)
    if profile is None:
        print(
            f"unknown profile {name!r}; known: {', '.join(p['name'] for p in profiles.PROFILES)}",
            file=sys.stderr,
        )
        return 2
    base_name = name.split("@", 1)[0]
    if active and active != base_name:
        print(f"warning: the Jetson runs {active}, the report will say {name}", file=sys.stderr)

    def progress(done: int, total: int, label: str) -> None:
        print(f"\r[{done:>3}/{total}] {label:<40}", end="", file=sys.stderr, flush=True)

    doc = benchkit.load_eval(args.eval)
    plan = benchkit.bench_plan(
        doc,
        profiles.sampling(profile),
        quick=args.quick,
        runs=args.runs or None,
        soak_minutes=args.soak_minutes,
        max_tokens=profile["max_output_tokens"],
        progress=progress,
    )
    poller.start()
    try:
        parts = benchkit.drive(plan, client.chat)
    finally:
        poller.stop()
        print(file=sys.stderr)

    report = benchkit.build_report(
        source=args.source,
        profile=profile,
        server={"url": args.url, "model_reported": client.model(), "cold_load_s": args.cold_load_s},
        parts=parts,
        host=benchkit.host_from_metrics(poller.last, args.llama_build),
        resources=poller.tracker.summary() if poller.tracker.samples else {},
        flags=args.flags,
    )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{date.today().isoformat()}_{name.replace('@', '-')}.json"
    out_file.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    speed, tool = report["speed"], report["toolcall"]
    print(f"profile            {name}")
    print(f"prefill / decode   {speed['prefill_tps']} / {speed['decode_tps']} tok/s")
    print(f"TTFT cold / warm   {speed['ttft_ms_cold']} / {speed['ttft_ms_warm']} ms")
    print(f"typical wake       {speed['e2e_wake_ms_warm']} ms")
    print(f"tool calls         {tool['passed']}/{tool['total']} passed, {tool['json_valid']} with clean JSON")
    for failure in tool["failures"][:10]:
        print(f"  FAIL {failure['id']}: {failure['reason']}")
    if report["errors"]:
        print(f"errors             {len(report['errors'])} (see the report)")
    print(f"report             {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
