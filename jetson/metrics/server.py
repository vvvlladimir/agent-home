#!/usr/bin/env python3
"""Jetson metrics and control endpoint: tegrastats → HTTP, plus the model-profile switch.

    GET  /metrics.json     contracts/schemas/jetson_metrics.schema.json (no auth, read-only facts)
    GET  /healthz
    POST /profile/<name>   Authorization: Bearer <token>; restarts llama-server with that profile

Standard library only, Python 3.10 (JetPack 6). Everything hardware-specific is optional: an
unknown tegrastats layout yields nulls and the raw line, never an exception. [UNVERIFIED-ON-HW]

Environment: AH_ETC (/etc/agent-house), AH_STATE (/var/lib/agent-house), AH_METRICS_HOST (0.0.0.0),
AH_METRICS_PORT (9100), AH_LLM_PORT (8080), AH_SYSROOT (tests only).
"""

from __future__ import annotations

import glob
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SYSROOT = os.environ.get("AH_SYSROOT", "")
ETC = Path(os.environ.get("AH_ETC", "/etc/agent-house"))
STATE = Path(os.environ.get("AH_STATE", "/var/lib/agent-house"))
PROFILES = Path(os.environ.get("AH_PROFILES_DIR", HERE.parent / "profiles"))
SWITCH = Path(os.environ.get("AH_SWITCH_SCRIPT", HERE.parent / "switch_profile.sh"))
DEFAULT_PROFILE = os.environ.get("AH_DEFAULT_PROFILE", "qwen35-4b-balanced")
LLM_PORT = int(os.environ.get("AH_LLM_PORT", "8080"))
PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

_RAM = re.compile(r"\bRAM (\d+)/(\d+)MB")
_SWAP = re.compile(r"\bSWAP (\d+)/(\d+)MB")
_CPU = re.compile(r"\bCPU \[([^\]]*)\]")
_GPU = re.compile(r"\bGR3D_FREQ (\d+)%")
_TEMP = re.compile(r"\b([A-Za-z0-9_]+)@(-?\d+(?:\.\d+)?)C\b")
_POWER = re.compile(r"\b(VDD_IN|POM_5V_IN) (\d+)mW")


def _read(path: str) -> str | None:
    try:
        return Path(SYSROOT + path).read_text(errors="replace").replace("\x00", "").strip()
    except OSError:
        return None


def parse_tegrastats(line: str) -> dict:
    """One tegrastats line → the hardware fields of the metrics document (missing = None)."""
    out: dict = dict.fromkeys(
        (
            "ram_used_mb",
            "ram_total_mb",
            "swap_used_mb",
            "swap_total_mb",
            "cpu_percent",
            "gpu_percent",
            "temp_cpu_c",
            "temp_gpu_c",
            "temp_tj_c",
            "power_mw",
        )
    )
    ram, swap = _RAM.search(line), _SWAP.search(line)
    if ram:
        out["ram_used_mb"], out["ram_total_mb"] = float(ram.group(1)), float(ram.group(2))
    if swap:
        out["swap_used_mb"], out["swap_total_mb"] = float(swap.group(1)), float(swap.group(2))
    cpu = _CPU.search(line)
    if cpu:
        loads = [float(m) for m in re.findall(r"(\d+)%@", cpu.group(1))]
        if loads:
            out["cpu_percent"] = round(sum(loads) / len(loads), 1)
    gpu = _GPU.search(line)
    if gpu:
        out["gpu_percent"] = float(gpu.group(1))
    temps = {name.lower(): float(value) for name, value in _TEMP.findall(line)}
    out["temp_cpu_c"] = temps.get("cpu")
    out["temp_gpu_c"] = temps.get("gpu")
    out["temp_tj_c"] = temps.get("tj")
    power = _POWER.search(line)
    if power:
        out["power_mw"] = float(power.group(2))
    return out


def read_procfs() -> dict:
    """Fallback when tegrastats is missing: RAM and swap from /proc/meminfo, temperatures from sysfs."""
    out = parse_tegrastats("")
    meminfo = {}
    for row in (_read("/proc/meminfo") or "").splitlines():
        key, _, rest = row.partition(":")
        value = rest.split()
        if value and value[0].isdigit():
            meminfo[key] = int(value[0]) / 1024  # kB → MB
    if "MemTotal" in meminfo and "MemAvailable" in meminfo:
        out["ram_total_mb"] = round(meminfo["MemTotal"], 1)
        out["ram_used_mb"] = round(meminfo["MemTotal"] - meminfo["MemAvailable"], 1)
    if "SwapTotal" in meminfo and "SwapFree" in meminfo:
        out["swap_total_mb"] = round(meminfo["SwapTotal"], 1)
        out["swap_used_mb"] = round(meminfo["SwapTotal"] - meminfo["SwapFree"], 1)
    for zone in glob.glob(SYSROOT + "/sys/class/thermal/thermal_zone*"):
        relative = zone[len(SYSROOT) :]
        kind, temp = (_read(relative + "/type") or "").lower(), _read(relative + "/temp")
        if not temp or not temp.lstrip("-").isdigit():
            continue
        for name in ("cpu", "gpu", "tj"):
            if kind.startswith(name):
                out[f"temp_{name}_c"] = int(temp) / 1000
    return out


def read_throttled() -> bool | None:
    """True when a thermal zone is at or above one of its passive trip points; None if unknown."""
    known = False
    for zone in glob.glob(SYSROOT + "/sys/class/thermal/thermal_zone*"):
        relative = zone[len(SYSROOT) :]
        temp = _read(relative + "/temp")
        if not temp or not temp.lstrip("-").isdigit():
            continue
        for kind_file in glob.glob(zone + "/trip_point_*_type"):
            trip = kind_file[len(SYSROOT) : -len("_type")]
            limit = _read(trip + "_temp")
            if _read(trip + "_type") == "passive" and limit and limit.isdigit():
                known = True
                if int(temp) >= int(limit):
                    return True
    return False if known else None


def l4t_version() -> tuple[str | None, int | None]:
    """('36.5.0', 6) from '# R36 (release), REVISION: 5.0, …'. JetPack major: R35→5, R36→6, R38+→7."""
    match = re.search(r"R(\d+) \(release\), REVISION: ([\d.]+)", _read("/etc/nv_tegra_release") or "")
    if not match:
        return None, None
    release = int(match.group(1))
    major = 5 if release == 35 else 6 if release == 36 else 7 if release >= 38 else None
    return f"{release}.{match.group(2)}", major


def _run(command: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        return done.returncode, (done.stdout + done.stderr).strip()
    except (OSError, subprocess.SubprocessError) as err:
        return 1, str(err)


class Sampler(threading.Thread):
    """Keeps the latest tegrastats line; falls back to procfs when tegrastats is not there."""

    def __init__(self, interval_ms: int = 2000) -> None:
        super().__init__(daemon=True, name="tegrastats")
        self.interval_ms = interval_ms
        self.line: str | None = None
        self.line_ts = 0.0
        self._slow: dict = {}
        self._slow_ts = 0.0
        self._lock = threading.Lock()
        release, major = l4t_version()
        self.static = {
            "hostname": socket.gethostname(),
            "module": _read("/proc/device-tree/model"),
            "l4t_version": release,
            "jetpack_major": major,
        }

    def run(self) -> None:
        binary = shutil.which("tegrastats")
        while binary:
            try:
                process = subprocess.Popen(  # noqa: S603
                    [binary, "--interval", str(self.interval_ms)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                for line in process.stdout or []:
                    self.line, self.line_ts = line.strip(), time.time()
                process.wait()
            except OSError:
                pass
            time.sleep(5)  # tegrastats exited: try again, serve procfs meanwhile

    def _slow_facts(self) -> dict:
        """Things that need a subprocess; refreshed at most every 5 s."""
        with self._lock:
            if time.time() - self._slow_ts < 5:
                return self._slow
            mode = None
            if shutil.which("nvpmodel"):
                match = re.search(r"NV Power Mode: (\S+)", _run(["nvpmodel", "-q"])[1])
                mode = match.group(1) if match else None
            state = (
                _run(["systemctl", "is-active", "llama-server.service"])[1]
                if shutil.which("systemctl")
                else ""
            )
            self._slow = {
                "power_mode": mode,
                "service_state": state.splitlines()[0] if state else None,
                "health": llama_health(),
            }
            self._slow_ts = time.time()
            return self._slow

    def metrics(self) -> dict:
        fresh = self.line is not None and time.time() - self.line_ts < 3 * self.interval_ms / 1000 + 5
        hardware = parse_tegrastats(self.line or "") if fresh else read_procfs()
        slow = self._slow_facts()
        return {
            "schema_version": 1,
            "ts": self.line_ts if fresh else time.time(),
            "source": "tegrastats" if fresh else "procfs",
            **self.static,
            "power_mode": slow["power_mode"],
            **hardware,
            "throttled": read_throttled(),
            "llama": {
                "active_profile": active_profile(),
                "service_state": slow["service_state"],
                "health": slow["health"],
            },
            "raw": self.line if fresh else None,
        }


def llama_health() -> str:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{LLM_PORT}/health", timeout=2) as response:  # noqa: S310
            return "ok" if response.status == 200 else "down"
    except urllib.error.HTTPError as err:
        return "loading" if err.code == 503 else "down"
    except (OSError, ValueError):
        return "down"


def active_profile() -> str | None:
    try:
        return Path(os.readlink(STATE / "active.env")).name[: -len(".env")]
    except OSError:
        return DEFAULT_PROFILE if (PROFILES / f"{DEFAULT_PROFILE}.env").is_file() else None


def read_token() -> str:
    try:
        return (ETC / "token").read_text().strip()
    except OSError:
        return ""


_switch_lock = threading.Lock()


def switch_profile(name: str) -> tuple[int, dict]:
    if not PROFILE_NAME.match(name) or not (PROFILES / f"{name}.env").is_file():
        return 404, {"ok": False, "error": "unknown profile"}
    if not _switch_lock.acquire(blocking=False):
        return 409, {"ok": False, "error": "another switch is in progress"}
    try:
        code, output = _run([str(SWITCH), name], timeout=75)
    finally:
        _switch_lock.release()
    if code != 0:
        return 500, {"ok": False, "error": output[-300:] or f"switch_profile.sh exited with {code}"}
    return 200, {"ok": True, "active_profile": name}


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-house-metrics"
    sampler: Sampler

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send(200, {"ok": True})
        elif path == "/metrics.json":
            try:
                payload = self.sampler.metrics()
            except Exception as err:  # noqa: BLE001 - answer, do not drop the connection
                traceback.print_exc()
                return self._send(500, {"ok": False, "error": f"{type(err).__name__}: {err}"[:300]})
            self._send(200, payload)
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not path.startswith("/profile/"):
            return self._send(404, {"ok": False, "error": "not found"})
        token = read_token()
        if not token:  # deny by default: no token on the Jetson = nobody may switch
            return self._send(403, {"ok": False, "error": "no token configured on the Jetson"})
        given = self.headers.get("Authorization", "")
        if not hmac.compare_digest(given.encode(), f"Bearer {token}".encode()):
            return self._send(401, {"ok": False, "error": "unauthorized"})
        status, payload = switch_profile(path[len("/profile/") :])
        return self._send(status, payload)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        if self.command != "GET":  # one line per switch attempt, silence for the polling
            sys.stderr.write(f"{self.address_string()} {format % args}\n")


def serve(host: str, port: int) -> ThreadingHTTPServer:
    Handler.sampler = Sampler()
    Handler.sampler.start()
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    host = os.environ.get("AH_METRICS_HOST", "0.0.0.0")  # noqa: S104 - the add-on on the Pi must reach it
    port = int(os.environ.get("AH_METRICS_PORT", "9100"))
    server = serve(host, port)
    print(f"agent-house metrics on http://{host}:{port}/metrics.json", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
