"""jetson/: setup.sh and setup_learner.sh against a fake sysroot (D40: the Jetson only learns, the
llama-server files are gone). Nothing here needs a Jetson."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]
JETSON = ROOT / "jetson"
SCHEMAS = ROOT / "contracts" / "schemas"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not installed")

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
    }
    if sysroot:
        env["AH_SYSROOT"] = str(sysroot)
    return env


# --- setup.sh ----------------------------------------------------------------------------


@needs_bash
def test_setup_check_json_on_a_jetson_changes_nothing(tmp_path, sysroot):
    run = subprocess.run(
        [BASH, JETSON / "setup.sh", "--check", "--json"],
        env=jetson_env(tmp_path, sysroot),
        capture_output=True,
        text=True,
    )
    report = json.loads(run.stdout)
    jsonschema.validate(report, json.loads((SCHEMAS / "setup_check.schema.json").read_text()))
    assert (
        report["is_orin_nano_8gb"] is True
        and report["jetpack_major"] == 6
        and report["l4t_version"] == "36.5.0"
    )
    assert report["zram_enabled"] is True and "llama_server_path" not in report
    checks = {c["name"]: c for c in report["checks"]}
    assert checks["platform"]["status"] == "ok" and checks["jetpack"]["status"] == "ok"
    assert checks["token"].get("would_change"), "the missing token is reported, not created"
    assert checks["unit:agent-house-learner.service"]["status"] == "warn"
    assert not any(name.startswith(("llama", "cuda", "unit:llama")) for name in checks)
    assert not (tmp_path / "etc").exists() and not (tmp_path / "state").exists()


@needs_bash
def test_setup_check_fails_on_a_non_jetson(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    run = subprocess.run(
        [BASH, JETSON / "setup.sh", "--check", "--json"],
        env=jetson_env(tmp_path, empty),
        capture_output=True,
        text=True,
    )
    checks = {c["name"]: c["status"] for c in json.loads(run.stdout)["checks"]}
    assert checks["platform"] == "fail"


@needs_bash
def test_setup_dry_run_prints_and_changes_nothing(tmp_path, sysroot):
    run = subprocess.run(
        [BASH, JETSON / "setup.sh", "--dry-run"],
        env=jetson_env(tmp_path, sysroot),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    assert "would run:" in run.stdout and "would write:" in run.stdout and "token" in run.stdout
    assert "llama" not in run.stdout and "build_llamacpp" not in run.stdout
    assert not (tmp_path / "etc").exists() and not (tmp_path / "state").exists()


@needs_bash
def test_an_old_llama_install_is_reported_and_stop_llm_disables_it(tmp_path, sysroot):
    units = sysroot / "etc" / "systemd" / "system"
    units.mkdir(parents=True)
    (units / "llama-server.service").write_text("[Unit]\n")
    run = subprocess.run(
        [BASH, JETSON / "setup.sh", "--check", "--json"], env=jetson_env(tmp_path, sysroot),
        capture_output=True, text=True,
    )  # fmt: skip
    checks = {c["name"]: c for c in json.loads(run.stdout)["checks"]}
    assert checks["old:llama-server.service"]["status"] == "warn"
    assert "--stop-llm" in checks["old:llama-server.service"]["would_change"]
    dry = subprocess.run(
        [BASH, JETSON / "learner" / "setup_learner.sh", "--dry-run", "--stop-llm"],
        env=jetson_env(tmp_path), capture_output=True, text=True,
    )  # fmt: skip
    assert dry.returncode == 0, dry.stderr
    assert "systemctl disable --now llama-server.service" in dry.stdout
    assert "systemctl disable --now agent-house-metrics.service" in dry.stdout
    assert "agent-house-learner.service" in dry.stdout


def test_no_llama_server_files_are_left():
    for gone in ("build_llamacpp.sh", "run_llama_server.sh", "switch_profile.sh", "llama-server.service",
                 "agent-house-metrics.service", "models.md", "profiles", "bench", "metrics"):  # fmt: skip
        assert not (JETSON / gone).exists(), gone



@needs_bash
def test_setup_rejects_bad_options(tmp_path):
    for args in (["--json"], ["--frobnicate"]):
        assert (
            subprocess.run(
                [BASH, JETSON / "setup.sh", *args], env=jetson_env(tmp_path), capture_output=True
            ).returncode
            == 2
        )
