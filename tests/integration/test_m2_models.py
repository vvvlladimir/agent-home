"""M2 acceptance: model profiles, the profile swap, the GUI benchmark and GUI-editable settings,
through the ingress prefix, against the fake llama-server and the fake Jetson."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import jsonschema
import pytest
import yaml

from sim import fake_jetson

from .test_m1_skeleton import OPENAPI, response_schema, wait_for

ROOT = Path(__file__).resolve().parents[2]
REPORT_SCHEMA = json.loads((ROOT / "contracts/schemas/bench_report.schema.json").read_text())


@pytest.fixture
def jetson_sim(llm_sim):
    """The fake Jetson restarts the fake llama-server on a profile switch (it answers 503 meanwhile)."""
    return fake_jetson.JetsonSim(llm_sim)


def validate(payload, path: str, method: str = "get", code: str = "200") -> None:
    jsonschema.validate(payload, response_schema(path, method, code), cls=jsonschema.Draft202012Validator)


def llm_ok(base: str) -> dict:
    return wait_for(lambda: (s := httpx.get(f"{base}/api/status").json())["llm"]["state"] == "ok" and s)


def test_profiles(ingress):
    llm_ok(ingress.base)
    listed = wait_for(
        lambda: (
            (p := httpx.get(f"{ingress.base}/api/models/profiles").json())
            and any(x["active"] for x in p)
            and p
        )
    )
    validate(listed, "/api/models/profiles")
    assert [p["name"] for p in listed if p["active"]] == ["qwen35-4b-balanced"]
    assert {"qwen35-2b-fast", "gemma4-e2b-compare", "qwen35-9b-experimental"} <= {p["name"] for p in listed}


def test_profile_swap_goes_warming_then_ok(ingress, jetson_sim, llm_sim):
    base = ingress.base
    llm_ok(base)
    jetson_sim.swap_seconds = 0.5
    response = httpx.put(f"{base}/api/models/active", json={"profile": "gemma4-e2b-compare"})
    assert response.status_code == 202
    validate(response.json(), "/api/models/active", "put", "202")
    assert response.json()["state"] == "warming" and response.json()["active_profile"] == "gemma4-e2b-compare"
    assert jetson_sim.profile_calls == ["gemma4-e2b-compare"]
    again = httpx.put(f"{base}/api/models/active", json={"profile": "qwen35-2b-fast"})
    assert again.status_code == 409, "one swap at a time"

    status = wait_for(
        lambda: (s := httpx.get(f"{base}/api/status").json())["llm"]["state"] == "ok" and s, timeout=10
    )
    assert status["llm"]["model"] == "gemma4-e2b-compare"
    wait_for(
        lambda: (
            [p["name"] for p in httpx.get(f"{base}/api/models/profiles").json() if p["active"]]
            == ["gemma4-e2b-compare"]
        )
    )
    summaries = [e["summary"] for e in httpx.get(f"{base}/api/events", params={"kind": "system"}).json()]
    assert any("switching to gemma4-e2b-compare" in s for s in summaries)


def test_profile_swap_errors(ingress, settings, jetson_sim):
    base = ingress.base
    llm_ok(base)
    assert httpx.put(f"{base}/api/models/active", json={"profile": "nope"}).status_code == 404
    jetson_sim.token = "rotated-on-the-jetson"
    response = httpx.put(f"{base}/api/models/active", json={"profile": "qwen35-2b-fast"})
    assert response.status_code == 502 and "401" in response.json()["detail"]
    assert httpx.get(f"{base}/api/status").json()["llm"]["state"] == "ok", (
        "a refused swap leaves the model as it was"
    )
    settings.jetson_token = ""
    response = httpx.put(f"{base}/api/models/active", json={"profile": "qwen35-2b-fast"})
    assert response.status_code == 409 and response.json()["error"] == "not_configured"


def test_gui_benchmark_runs_once_at_a_time_and_stores_a_valid_report(ingress, llm_sim):
    base = ingress.base
    llm_ok(base)
    llm_sim.latency = 0.02  # long enough to see it running
    response = httpx.post(f"{base}/api/benchmarks", json={"quick": True})
    assert response.status_code == 202
    run_id = response.json()["id"]
    assert httpx.post(f"{base}/api/benchmarks", json={"quick": True}).status_code == 409
    assert httpx.put(f"{base}/api/models/active", json={"profile": "qwen35-2b-fast"}).status_code == 409
    wrong = httpx.post(f"{base}/api/benchmarks", json={"profile": "qwen35-2b-fast"})
    assert wrong.status_code == 409

    listed = httpx.get(f"{base}/api/benchmarks").json()
    validate(listed, "/api/benchmarks")
    assert listed[0]["id"] == run_id and listed[0]["status"] == "running"

    done = wait_for(
        lambda: (b := httpx.get(f"{base}/api/benchmarks").json())[0]["status"] != "running" and b, timeout=30
    )
    validate(done, "/api/benchmarks")
    run = done[0]
    assert run["status"] == "done" and "progress" not in run
    jsonschema.validate(run["report"], REPORT_SCHEMA, format_checker=jsonschema.FormatChecker())
    assert run["report"]["source"] == "addon" and run["report"]["toolcall"]["total"] == 12
    assert run["report"]["resources"]["ram_peak_mb"] == 4600.0
    assert all(r.get("presence_penalty") == 1.5 for r in llm_sim.requests if r.get("tools"))


def test_benchmark_refused_when_the_model_is_not_ready(ingress, llm_sim):
    base = ingress.base
    llm_ok(base)
    llm_sim.loading = True
    wait_for(lambda: httpx.get(f"{base}/api/status").json()["llm"]["state"] == "warming", timeout=15)
    response = httpx.post(f"{base}/api/benchmarks", json={})
    assert response.status_code == 409 and "warming" in response.json()["detail"]


def test_put_settings(ingress, settings, app_server):
    base = ingress.base
    shown = httpx.put(f"{base}/api/settings", json={"llm_timeout_s": 60, "llm_presence_penalty": 0}).json()
    validate(shown, "/api/settings", "put")
    assert shown["runtime"]["llm_timeout_s"] == 60 and shown["runtime"]["llm_presence_penalty"] == 0
    stored = yaml.safe_load((settings.data_dir / "settings.yaml").read_text())
    assert stored["llm_timeout_s"] == 60
    ctx = app_server.app.state.ctx
    assert ctx.llm.runtime.llm_timeout_s == 60, "the LLM manager uses the new values at once"
    assert ctx.llm.request_body([])["presence_penalty"] == 0

    back = httpx.put(f"{base}/api/settings", json={"llm_presence_penalty": None}).json()
    assert back["runtime"]["llm_presence_penalty"] is None and back["runtime"]["llm_timeout_s"] == 60

    for bad in ({"llm_timeout_s": 1}, {"anthropic_api_key": "x"}, {"llm_max_queue": "many"}, [1], "x"):
        response = httpx.put(f"{base}/api/settings", json=bad)
        assert response.status_code == 422 and response.json()["error"] == "invalid", bad
    assert (
        httpx.put(
            f"{base}/api/settings", content=b"{not json", headers={"content-type": "application/json"}
        ).status_code
        == 422
    )
    assert settings.anthropic_api_key == "", "add-on options are not reachable from the GUI"
    assert any(op.get("x-milestone") == "M2" for op in OPENAPI["paths"]["/api/settings"].values())
