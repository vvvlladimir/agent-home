"""contracts/ must stay valid: every agent codes against it."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from openapi_spec_validator import validate

ROOT = Path(__file__).resolve().parents[2]
DONE = {"M1", "M2"}  # milestones whose operations must all be routed
SCHEMAS = sorted((ROOT / "contracts" / "schemas").glob("*.schema.json"))
MILESTONES = {"M1", "M2", "M3", "M4", "M5"}


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


@pytest.mark.parametrize("path", SCHEMAS, ids=lambda p: p.name)
def test_json_schemas_are_valid(path):
    schema = json.loads(path.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$id"] == path.name


def test_agent_config_example_and_defaults():
    schema = json.loads((ROOT / "contracts/schemas/agent_config.schema.json").read_text())
    example = {
        "id": "kitchen",
        "name": "Kitchen",
        "room": "kitchen",
        "entities": ["light.kitchen", "binary_sensor.kitchen_motion"],
        "wake_rules": {
            "events": [{"entities": ["binary_sensor.kitchen_motion"], "to": ["on"], "cooldown_s": 60}],
            "cron": [{"expr": "0 7 * * *", "reason": "morning check"}],
        },
    }
    jsonschema.validate(example, schema)
    assert schema["properties"]["mode"]["default"] == "shadow", "agents must default to shadow mode"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**example, "mode": "yolo"}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**example, "entities": [f"light.l{i}" for i in range(26)]}, schema)


def test_implemented_operations_are_not_answered_by_the_501_fallback():
    """Guards against the contract and the router drifting apart."""
    from app.api.routes import router

    spec = yaml.safe_load((ROOT / "contracts" / "openapi.yaml").read_text())
    routes = {(r.path, m.lower()) for r in router.routes for m in r.methods if "{path:path}" not in r.path}
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method != "parameters" and operation["x-milestone"] in DONE:
                assert (path, method) in routes, f"{method} {path} is {operation['x-milestone']} but not implemented"
