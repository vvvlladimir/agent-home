"""jetson/: profiles in step with the add-on, scripts against a fake sysroot, the metrics endpoint,
and bench.py end to end against the fake llama-server. Nothing here needs a Jetson."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import httpx
import jsonschema
import pytest

from app.core import benchkit, profiles
from sim import fake_jetson
from sim.eval_oracle import script_for_bench

ROOT = Path(__file__).resolve().parents[2]
JETSON = ROOT / "jetson"
SCHEMAS = ROOT / "contracts" / "schemas"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not installed")

TEGRASTATS = (
    "11-13-2026 10:00:00 RAM 4512/7620MB (lfb 2x4MB) SWAP 12/3810MB (cached 0MB) "
    "CPU [12%@1728,8%@1728,5%@1728,3%@1728,off,off] GR3D_FREQ 45% cpu@52.5C soc2@50.1C "
    "soc0@50.9C gpu@51.2C tj@53.1C soc1@50.3C VDD_IN 9812mW/9812mW VDD_CPU_GPU_CV 2010mW/2010mW"
)


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out[key] = value.strip("'\"")
    return out


@pytest.fixture
def sysroot(tmp_path) -> Path:
    """The files an Orin Nano 8 GB on JetPack 6 shows to the read-only probes."""
    root = tmp_path / "sysroot"
    files = {
        "proc/device-tree/model": "NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super\0",
        "etc/nv_tegra_release": "# R36 (release), REVISION: 5.0, GCID: 1, BOARD: generic, EABI: aarch64\n",
        "etc/os-release": 'NAME="Ubuntu"\nVERSION_ID="22.04"\n',
        "proc/meminfo": "MemTotal:        7802348 kB\nMemAvailable:    5120000 kB\n"
        "SwapTotal:       3901172 kB\nSwapFree:        3901172 kB\n",
        "proc/swaps": "Filename Type Size Used Priority\n/dev/zram0 partition 1950584 0 5\n",
        "usr/local/cuda/version.json": '{"cuda": {"name": "CUDA SDK", "version": "12.6.68"}}',
        "sys/class/thermal/thermal_zone0/type": "cpu-thermal\n",
        "sys/class/thermal/thermal_zone0/temp": "52500\n",
        "sys/class/thermal/thermal_zone0/trip_point_0_type": "passive\n",
        "sys/class/thermal/thermal_zone0/trip_point_0_temp": "95500\n",
    }
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content)
    return root


def jetson_env(tmp_path: Path, sysroot: Path | None = None) -> dict[str, str]:
    env = {
        **os.environ,
        "AH_ETC": str(tmp_path / "etc"),
        "AH_STATE": str(tmp_path / "state"),
        "LLAMA_SERVER_BIN": str(tmp_path / "no-llama-server"),
    }
    if sysroot:
        env["AH_SYSROOT"] = str(sysroot)
    return env


# --- profiles ----------------------------------------------------------------------------


def test_profile_files_match_the_addon_catalogue():
    files = {p.stem: read_env(p) for p in (JETSON / "profiles").glob("*.env")}
    assert set(files) == {p["name"] for p in profiles.PROFILES}
    assert set(fake_jetson.JetsonSim().profiles) == set(files)
    for profile in profiles.PROFILES:
        env = files[profile["name"]]
        assert env["PROFILE_NAME"] == profile["name"]
        assert env["MODEL_HF"] == profile["model"]
        assert int(env["CTX_SIZE"]) == profile["ctx_size"]
        assert env["PARALLEL"] == "1", "one slot: the LLM manager serialises requests"
        assert (env["THINKING"] == "off") == (profile["thinking"] is False and "qwen" in profile["name"])
    assert profiles.DEFAULT_PROFILE in files
    assert "gemma4-e2b-compare" in files


@needs_bash
@pytest.mark.parametrize("name", ["qwen35-4b-balanced", "gemma4-e2b-compare"])
def test_llama_server_command_line(tmp_path, name):
    env = {**jetson_env(tmp_path), "AH_PROFILE_FILE": str(JETSON / "profiles" / f"{name}.env")}
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "token").write_text("secret\n")
    out = subprocess.run(
        [BASH, JETSON / "run_llama_server.sh", "--print-cmd"], env=env, capture_output=True, text=True, check=True
    ).stdout
    profile = profiles.get_profile(name)
    assert f"-hf {profile['model']}" in out and f"--alias {name}" in out and "-np 1" in out
    assert f"--api-key-file {tmp_path / 'etc' / 'token'}" in out
    assert ("--reasoning-budget 0" in out) == name.startswith("qwen")
    assert "secret" not in out


@needs_bash
def test_switch_profile_without_restart(tmp_path):
    env = jetson_env(tmp_path)
    run = subprocess.run([BASH, JETSON / "switch_profile.sh", "qwen35-2b-fast", "--no-restart"], env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert os.readlink(tmp_path / "state" / "active.env").endswith("qwen35-2b-fast.env")
    for bad in ("../../etc/passwd", "nope", ""):
        assert subprocess.run([BASH, JETSON / "switch_profile.sh", bad, "--no-restart"], env=env, capture_output=True).returncode != 0


# --- setup.sh ----------------------------------------------------------------------------


@needs_bash
def test_setup_check_json_on_a_jetson_changes_nothing(tmp_path, sysroot):
    run = subprocess.run(
        [BASH, JETSON / "setup.sh", "--check", "--json"], env=jetson_env(tmp_path, sysroot), capture_output=True, text=True
    )
    report = json.loads(run.stdout)
    jsonschema.validate(report, json.loads((SCHEMAS / "setup_check.schema.json").read_text()))
    assert report["is_orin_nano_8gb"] is True and report["jetpack_major"] == 6 and report["l4t_version"] == "36.5.0"
    assert report["cuda_version"] == "12.6.68" and report["zram_enabled"] is True
    checks = {c["name"]: c for c in report["checks"]}
    assert checks["platform"]["status"] == "ok" and checks["jetpack"]["status"] == "ok"
    assert checks["token"].get("would_change"), "the missing token is reported, not created"
    assert not (tmp_path / "etc").exists() and not (tmp_path / "state").exists()


@needs_bash
def test_setup_check_fails_on_a_non_jetson(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    run = subprocess.run([BASH, JETSON / "setup.sh", "--check", "--json"], env=jetson_env(tmp_path, empty), capture_output=True, text=True)
    checks = {c["name"]: c["status"] for c in json.loads(run.stdout)["checks"]}
    assert checks["platform"] == "fail"


@needs_bash
def test_setup_dry_run_prints_and_changes_nothing(tmp_path, sysroot):
    run = subprocess.run([BASH, JETSON / "setup.sh", "--dry-run"], env=jetson_env(tmp_path, sysroot), capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert "would run:" in run.stdout and "would write:" in run.stdout
    assert "libssl-dev" in (JETSON / "setup.sh").read_text(), "without it llama.cpp is built without HTTPS"
    assert not (tmp_path / "etc").exists() and not (tmp_path / "state").exists()


@needs_bash
def test_llamacpp_build_has_https_for_model_downloads(tmp_path):
    run = subprocess.run([BASH, JETSON / "build_llamacpp.sh", "--dry-run", "--dir", str(tmp_path / "llama.cpp")],
                         env=jetson_env(tmp_path), capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert "-DLLAMA_OPENSSL=ON" in run.stdout and "-DCMAKE_CUDA_ARCHITECTURES=87" in run.stdout


@needs_bash
def test_setup_rejects_bad_options(tmp_path):
    for args in (["--json"], ["--frobnicate"]):
        assert subprocess.run([BASH, JETSON / "setup.sh", *args], env=jetson_env(tmp_path), capture_output=True).returncode == 2


# --- metrics endpoint --------------------------------------------------------------------


@pytest.fixture
def metrics(tmp_path, monkeypatch, sysroot, llm_server):
    server = load(JETSON / "metrics" / "server.py", "jetson_metrics_server")
    switched = tmp_path / "switched"
    script = tmp_path / "switch.sh"
    script.write_text(f'#!/bin/sh\necho "$1" > {switched}\n')
    script.chmod(0o755)
    monkeypatch.setattr(server, "SYSROOT", str(sysroot))
    monkeypatch.setattr(server, "ETC", tmp_path / "etc")
    monkeypatch.setattr(server, "STATE", tmp_path / "state")
    monkeypatch.setattr(server, "SWITCH", script)
    monkeypatch.setattr(server, "LLM_PORT", int(llm_server.url.rsplit(":", 1)[1]))
    httpd = server.serve("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    httpd.module, httpd.switched = server, switched
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield httpd
    httpd.shutdown()


def test_tegrastats_parsing():
    server = load(JETSON / "metrics" / "server.py", "jetson_metrics_server")
    parsed = server.parse_tegrastats(TEGRASTATS)
    assert parsed == {
        "ram_used_mb": 4512.0,
        "ram_total_mb": 7620.0,
        "swap_used_mb": 12.0,
        "swap_total_mb": 3810.0,
        "cpu_percent": 7.0,
        "gpu_percent": 45.0,
        "temp_cpu_c": 52.5,
        "temp_gpu_c": 51.2,
        "temp_tj_c": 53.1,
        "power_mw": 9812.0,
    }
    assert set(server.parse_tegrastats("something else entirely").values()) == {None}


def test_metrics_document(metrics):
    doc = httpx.get(f"{metrics.url}/metrics.json").json()
    jsonschema.validate(doc, json.loads((SCHEMAS / "jetson_metrics.schema.json").read_text()))
    assert doc["source"] == "procfs" and doc["jetpack_major"] == 6
    assert doc["ram_total_mb"] == pytest.approx(7619.5, abs=1) and doc["temp_cpu_c"] == 52.5
    assert doc["throttled"] is False
    assert doc["llama"]["health"] == "ok" and doc["llama"]["active_profile"] == profiles.DEFAULT_PROFILE

    metrics.module.Handler.sampler.line, metrics.module.Handler.sampler.line_ts = TEGRASTATS, __import__("time").time()
    doc = httpx.get(f"{metrics.url}/metrics.json").json()
    assert doc["source"] == "tegrastats" and doc["gpu_percent"] == 45.0 and doc["raw"] == TEGRASTATS
    assert httpx.get(f"{metrics.url}/healthz").json() == {"ok": True}
    assert httpx.get(f"{metrics.url}/nope").status_code == 404


def test_profile_switch_needs_the_token(metrics):
    url = f"{metrics.url}/profile/qwen35-2b-fast"
    assert httpx.post(url).status_code == 403, "no token on the Jetson = nobody may switch"
    (metrics.module.ETC).mkdir()
    (metrics.module.ETC / "token").write_text("tok\n")
    assert httpx.post(url).status_code == 401
    assert httpx.post(url, headers={"Authorization": "Bearer wrong"}).status_code == 401
    auth = {"Authorization": "Bearer tok"}
    assert httpx.post(f"{metrics.url}/profile/..%2F..%2Fetc", headers=auth).status_code == 404
    assert httpx.post(f"{metrics.url}/profile/unknown", headers=auth).status_code == 404
    assert not metrics.switched.exists()
    response = httpx.post(url, headers=auth)
    assert response.json() == {"ok": True, "active_profile": "qwen35-2b-fast"}
    assert metrics.switched.read_text().strip() == "qwen35-2b-fast"


def test_a_failing_switch_is_reported(metrics):
    metrics.module.ETC.mkdir()
    (metrics.module.ETC / "token").write_text("tok")
    metrics.module.SWITCH.write_text("#!/bin/sh\necho 'sudo: a password is required' >&2\nexit 1\n")
    response = httpx.post(f"{metrics.url}/profile/qwen35-2b-fast", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 500 and "password" in response.json()["error"]


# --- bench.py ----------------------------------------------------------------------------


def test_bench_py_end_to_end_against_the_fake_server(tmp_path, llm_sim, llm_server, jetson_server):
    doc = benchkit.load_eval()
    llm_sim.script.extend(script_for_bench(doc, benchkit.pick_scenarios(doc, True), 1))
    bench = load(JETSON / "bench" / "bench.py", "jetson_bench")
    sample = tmp_path / "sample.txt"
    code = bench.main(
        [
            "--url", llm_server.url,
            "--metrics-url", jetson_server.url,
            "--token-file", str(tmp_path / "missing"),
            "--out", str(tmp_path / "out"),
            "--source", "sim",
            "--quick",
            "--save-sample", str(sample),
        ]
    )
    assert code == 0
    (report_file,) = (tmp_path / "out").glob("*_qwen35-4b-balanced.json")
    report = json.loads(report_file.read_text())
    jsonschema.validate(report, json.loads((SCHEMAS / "bench_report.schema.json").read_text()))
    assert report["source"] == "sim" and report["profile"]["name"] == "qwen35-4b-balanced"
    assert report["toolcall"]["passed"] == report["toolcall"]["total"] == 12
    assert report["resources"]["ram_peak_mb"] == 4600.0
    assert "tool_calls" in sample.read_text(), "one raw tool-call response is kept for the owner"


def test_bench_py_refuses_an_unhealthy_server_and_unknown_profiles(tmp_path, llm_server):
    bench = load(JETSON / "bench" / "bench.py", "jetson_bench")
    common = ["--metrics-url", "", "--token-file", str(tmp_path / "x"), "--out", str(tmp_path)]
    assert bench.main(["--url", llm_server.url, "--profile", "nope", *common]) == 2
    bench.Client.wait_healthy = lambda self, seconds: False
    assert bench.main(["--url", "http://127.0.0.1:9", *common]) == 2


def test_a_metrics_bug_is_answered_not_a_dropped_connection(metrics, monkeypatch):
    """Owner diagnostics 2026-11-13: "RemoteProtocolError: Server disconnected without sending a
    response" is what an exception inside do_GET looks like from the Pi."""

    def broken():
        raise KeyError("power_mode")

    monkeypatch.setattr(metrics.module.Handler.sampler, "metrics", broken)
    response = httpx.get(f"{metrics.url}/metrics.json")
    assert response.status_code == 500 and "KeyError" in response.json()["error"]
