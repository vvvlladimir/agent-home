"""`make analyze-report`: recommends only from hardware reports that meet every target."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from app.core import benchkit
from sim.eval_oracle import script_for_bench

ROOT = Path(__file__).resolve().parents[2]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sim_report(tmp_path, llm_sim, llm_server) -> dict:
    doc = benchkit.load_eval()
    llm_sim.script.extend(script_for_bench(doc, benchkit.pick_scenarios(doc, True), 1))
    bench = load(ROOT / "jetson" / "bench" / "bench.py", "jetson_bench_for_analyze")
    out = tmp_path / "made"
    common = ["--metrics-url", "", "--token-file", str(tmp_path / "x"), "--out", str(out), "--source", "sim"]
    assert bench.main(["--url", llm_server.url, "--quick", *common]) == 0
    (path,) = out.glob("*.json")
    return json.loads(path.read_text())


def hardware(report: dict, name: str, **changes) -> dict:
    measured = copy.deepcopy(report)
    measured["source"] = "jetson"
    measured["profile"]["name"] = name
    measured["resources"] = {"ram_peak_mb": 5000, "ram_total_mb": 7620, "ram_free_min_mb": 2600, "swap_peak_mb": 0}
    measured["speed"]["e2e_wake_ms_warm"] = 3000.0
    for key, value in changes.items():
        section, field = key.split("__")
        measured[section][field] = value
    return measured


def write(reports: Path, name: str, report: dict) -> None:
    (reports / "bench").mkdir(parents=True, exist_ok=True)
    (reports / "bench" / name).write_text(json.dumps(report))


def test_simulator_reports_never_recommend(tmp_path, sim_report):
    analyze = load(ROOT / "tools" / "analyze_report.py", "analyze_report").analyze
    write(tmp_path, "sim.json", sim_report)
    block = analyze(tmp_path)
    assert "n/a (simulator)" in block and "Only simulator reports" in block and "Recommendation" not in block


def test_recommends_the_most_accurate_qualifying_profile(tmp_path, sim_report):
    analyze = load(ROOT / "tools" / "analyze_report.py", "analyze_report").analyze
    write(tmp_path, "a.json", hardware(sim_report, "qwen35-4b-balanced", speed__decode_tps=15.0))
    write(tmp_path, "b.json", hardware(sim_report, "qwen35-2b-fast", speed__decode_tps=30.0, toolcall__passed=10))
    write(tmp_path, "c.json", hardware(sim_report, "qwen35-9b-experimental", resources__ram_free_min_mb=300))
    write(tmp_path, "broken.json", {"kind": "nope"})
    block = analyze(tmp_path)
    assert "**Recommendation:** `qwen35-4b-balanced`" in block
    assert "minimum free RAM 300 MB" in block and "tool-call accuracy 83%" in block
    assert "broken.json" in block.split("did not validate")[1]


def test_no_qualifying_profile_keeps_the_default(tmp_path, sim_report):
    analyze = load(ROOT / "tools" / "analyze_report.py", "analyze_report").analyze
    write(tmp_path, "slow.json", hardware(sim_report, "qwen35-4b-balanced", speed__e2e_wake_ms_warm=12000.0))
    assert "no measured profile meets all targets" in analyze(tmp_path)
