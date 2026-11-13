"""A perfect model for the tool-call eval: scripted fake-LLM steps derived from each scenario's
expectations. Lets tests prove the harness and the scorer without a real model."""

from __future__ import annotations


def _first(value):
    if isinstance(value, list):
        return _first(value[0])
    if isinstance(value, dict):
        return {key: _first(item) for key, item in value.items()}
    return value


def steps_for(scenario: dict) -> list[dict]:
    """fake_llm script steps for one scenario: one turn with every expected call, then the answer."""
    calls = []
    for pattern in scenario["expect"].get("calls") or []:
        args = _first(pattern.get("args") or {})
        if pattern["tool"] == "call_service":
            args.setdefault("domain", args["entity_id"].split(".", 1)[0])
        elif pattern["tool"] == "report":
            args = {"severity": "info", "summary": "Scripted.", **args}
        calls.append({"name": pattern["tool"], "arguments": args})
    return ([{"tool_calls": calls}] if calls else []) + [{"text": "Done."}]


def script_for_bench(doc: dict, scenarios: list[dict], runs: int) -> list[dict]:
    """The whole request sequence of benchkit.bench_plan: speed phase, wakes, then the eval."""
    script = [{"text": "ok"} for _ in range(runs + 1 + runs)]  # cold, prime, warm
    script += [{"text": " ".join(str(n) for n in range(1, 60))} for _ in range(runs)]  # decode
    for _ in range(runs):
        script += steps_for(doc["scenarios"][0])
    for scenario in scenarios:
        script += steps_for(scenario)
    return script
