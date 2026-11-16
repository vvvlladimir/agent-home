"""contracts/ must stay valid: every agent codes against it."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from openapi_spec_validator import validate

ROOT = Path(__file__).resolve().parents[2]
DONE = {"M1", "M2", "M3", "M4", "M5", "M6"}  # milestones whose operations must all be routed
SCHEMAS = sorted((ROOT / "contracts" / "schemas").glob("*.schema.json"))
MILESTONES = {"M1", "M2", "M3", "M4", "M5", "M6"}


def test_openapi_is_valid_and_every_operation_has_a_milestone():
    spec = yaml.safe_load((ROOT / "contracts" / "openapi.yaml").read_text())
    validate(spec)
    seen = set()
    for path, item in spec["paths"].items():
        assert path.startswith("/api/"), path
        for method, operation in item.items():
            if method == "parameters":
                continue
            assert operation.get("x-milestone") in MILESTONES, f"{method} {path}"
            assert operation["operationId"] not in seen, operation["operationId"]
            seen.add(operation["operationId"])


def test_learner_openapi_is_valid():
    """The Jetson learner's own API (D38): every path but /healthz needs the bearer token."""
    spec = yaml.safe_load((ROOT / "contracts" / "learner.openapi.yaml").read_text())
    validate(spec)
    assert spec["security"] == [{"bearer": []}]
    for path, item in spec["paths"].items():
        for operation in item.values():
            assert (operation.get("security") == []) == (path == "/healthz"), path


@pytest.mark.parametrize("path", SCHEMAS, ids=lambda p: p.name)
def test_json_schemas_are_valid(path):
    schema = json.loads(path.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$id"] == path.name


def test_the_contract_and_the_routes_match_both_ways():
    """Guards against the contract and the router drifting apart: every operation of a finished
    milestone is routed, and every route is in the contract (the GUI codes against it, D40)."""
    from app.api.activity import router as activity_router
    from app.api.automations import router as automations_router
    from app.api.chat import router as chat_router
    from app.api.council import router as council_router
    from app.api.dev import router as dev_router
    from app.api.migration import router as migration_router
    from app.api.routes import router

    spec = yaml.safe_load((ROOT / "contracts" / "openapi.yaml").read_text())
    routes = {
        (r.path, m.lower())
        for r in (
            *activity_router.routes,
            *dev_router.routes,
            *router.routes,
            *automations_router.routes,
            *migration_router.routes,
            *chat_router.routes,
            *council_router.routes,
        )
        for m in r.methods
        if "{path:path}" not in r.path
    }
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method != "parameters" and operation["x-milestone"] in DONE:
                assert (path, method) in routes, (
                    f"{method} {path} is {operation['x-milestone']} but not implemented"
                )
    documented = {(path, m) for path, item in spec["paths"].items() for m in item if m != "parameters"}
    undocumented = sorted(r for r in routes - documented if r[1] != "head")
    assert undocumented == [], f"routes missing from contracts/openapi.yaml: {undocumented}"
    for gone in ("/api/agents", "/api/models/profiles", "/api/benchmarks", "/api/runs/{run_id}/replay"):
        assert gone not in spec["paths"], f"{gone} went with the room agents (D40)"
