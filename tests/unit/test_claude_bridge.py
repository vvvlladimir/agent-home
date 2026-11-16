"""sim/claude_bridge: `claude -p` as the polygon's model (D30). The CLI is faked; no network."""

from __future__ import annotations

import json
import subprocess

import pytest

from sim import claude_bridge as bridge


def stream(result: dict, *tools: str) -> str:
    """`--output-format stream-json`: one event per line, the tool uses, then the result."""
    events = [{"type": "system", "subtype": "init"}]
    events += [{"type": "assistant", "message": {"id": "m1", "content": [
        {"type": "tool_use", "name": n, "input": {"query": "lamp"} if n != "StructuredOutput" else {}}]}}
        for n in tools]  # fmt: skip
    events += [{"type": "assistant", "message": {"id": "m2", "content": [
        {"type": "tool_use", "name": "StructuredOutput", "input": {}}]}}]  # fmt: skip
    return "\n".join(json.dumps(e) for e in [*events, {"type": "result", **result}])


class FakeCLI:
    """Stands in for `subprocess.run(["claude", "-p", ...])`; records argv, prompt and schema."""

    def __init__(self, out: dict | None, **extra):
        self.out, self.extra, self.calls = out, extra, []

    def __call__(self, cmd, input, **kw):  # noqa: A002 - subprocess.run's own name
        self.calls.append({"cmd": cmd, "prompt": input, "env": kw.get("env") or {}})
        data = {"is_error": False, "structured_output": self.out, "usage": {"input_tokens": 10,
                "cache_read_input_tokens": 90, "output_tokens": 7},
                "modelUsage": {"claude-haiku-5-5": {}}, **self.extra}  # fmt: skip
        return subprocess.CompletedProcess(cmd, 0, stream(data), "")

    def flag(self, name: str) -> str:
        cmd = self.calls[-1]["cmd"]
        return cmd[cmd.index(name) + 1]


@pytest.fixture
def cli(monkeypatch):
    def install(out, **extra):
        fake = FakeCLI(out, **extra)
        monkeypatch.setattr(bridge.subprocess, "run", fake)
        monkeypatch.setattr(bridge, "available", lambda: True)
        return fake

    return install


TOOLS = [{"type": "function", "function": {"name": "call_service", "description": "HA service",
          "parameters": {"type": "object", "properties": {"entity_id": {"type": "string"}}}}}]  # fmt: skip
CHAT = {
    "messages": [
        {"role": "system", "content": "You run the kitchen."},
        {"role": "user", "content": "motion in the kitchen"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
         "function": {"name": "call_service", "arguments": "{\"entity_id\": \"light.k\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "shadow: logged"},
    ],
    "tools": TOOLS,
}  # fmt: skip


def test_orchestrator_messages_round_trip(cli):
    fake = cli({"text": "drafting", "tool_calls": [{"name": "submit_draft", "arguments": {"yaml": "x"}}]})
    body = {
        "model": "claude-opus-5-5", "max_tokens": 4000,
        "system": [{"type": "text", "text": "You are the orchestrator.", "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "lights off at 23:00"},
                     {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "list_entities",
                                                        "input": {}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                   "content": "light.a", "is_error": True}]}],
        "tools": [{"name": "submit_draft", "description": "d", "input_schema": {"type": "object"}},
                  {"name": "list_entities", "description": "e", "input_schema": {"type": "object"}}],
    }  # fmt: skip
    resp = bridge.messages(body, "sonnet", "medium")
    assert resp["stop_reason"] == "tool_use" and resp["model"] == "claude-haiku-5-5"
    assert [b["type"] for b in resp["content"]] == ["text", "tool_use"]
    assert resp["content"][1]["input"] == {"yaml": "x"} and resp["usage"] == {
        "input_tokens": 100,
        "output_tokens": 7,
    }
    assert "You are the orchestrator." in fake.flag("--system-prompt")
    assert "TOOL RESULT id=t1 (error):\nlight.a" in fake.calls[-1]["prompt"]


def test_failures_raise_a_bridge_error(cli, monkeypatch):
    cli(None, is_error=True, result="usage limit reached")
    with pytest.raises(bridge.BridgeError, match="usage limit"):
        bridge.ask("same?")

    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(bridge.subprocess, "run", hang)
    with pytest.raises(bridge.BridgeError, match="timed out"):
        bridge.ask("same?")
    monkeypatch.setattr(bridge, "available", lambda: False)
    with pytest.raises(bridge.BridgeError, match="not on PATH"):
        bridge.ask("same?")


def test_the_orchestrator_gateway_answers_through_the_bridge(cli, tmp_path):
    from anthropic import Anthropic

    from sim.gateways import Gateways
    from sim.server import ServerThread
    from sim.sim_config import SimConfig

    cfg = SimConfig.load(tmp_path)
    cfg.update({"anthropic": {"backend": "claude", "claude_model": "opus", "claude_effort": "high"}})
    with pytest.raises(ValueError, match="Anthropic gateway only"):
        cfg.update({"openai": {"backend": "claude"}})
    gw = Gateways(cfg, {})
    claude = ServerThread(gw.cloud_app("anthropic"))
    claude.start()
    try:
        # the add-on's own SDK parses the answer, so the shape matches the Messages API; no key needed
        fake = cli({"text": "ok", "tool_calls": []})
        resp = Anthropic(api_key="sk-ant-sim", base_url=claude.url, max_retries=0).messages.create(
            model="claude-opus-5-5", max_tokens=100, messages=[{"role": "user", "content": "hi"}]
        )
        assert resp.content[0].text == "ok" and resp.stop_reason == "end_turn"
        assert fake.flag("--model") == "opus" and fake.flag("--effort") == "high"
        assert fake.flag("--tools") == "" and "--no-session-persistence" in fake.calls[-1]["cmd"]
        assert [row["backend"] for row in gw.log.list()] == ["claude"]
    finally:
        claude.stop()


def test_a_turn_that_reached_for_a_native_tool_is_retried_then_refused(monkeypatch):
    """Stories 2026-11-15: with no CLI tools the model sometimes tried one, got "No such tool
    available" and told the owner its call_service was broken. Such an answer is never used; a
    retried StructuredOutput (an extra turn, nothing native) is a clean answer."""
    answers = iter([("Bash",), ()])

    def fake(cmd, input, **kw):  # noqa: A002
        data = {"is_error": False, "num_turns": 3, "structured_output": {"text": "ok", "tool_calls": []},
                "usage": {"input_tokens": 5, "output_tokens": 1}}  # fmt: skip
        return subprocess.CompletedProcess(cmd, 0, stream(data, *next(answers)), "")

    monkeypatch.setattr(bridge.subprocess, "run", fake)
    monkeypatch.setattr(bridge, "available", lambda: True)
    res = bridge.run("sys", "hi", {}, "haiku", "low")
    assert res["out"]["text"] == "ok" and res["tokens_in"] == 10 and res["tokens_out"] == 2
    answers = iter([("Bash",), ("Read",)])
    with pytest.raises(bridge.BridgeError, match=r"native tools \(Read\)"):
        bridge.run("sys", "hi", {}, "haiku", "low")


def test_a_native_call_of_the_applications_own_tool_is_taken_as_the_tool_call(monkeypatch):
    """The model called find_entities natively (CLI: "No such tool"), then wrote that its tools
    were broken. The call is what it meant: it becomes tool_calls, the text is dropped."""

    def fake(cmd, input, **kw):  # noqa: A002
        data = {"is_error": False, "structured_output": {"text": "every tool call failed", "tool_calls": []},
                "usage": {"input_tokens": 5, "output_tokens": 1}}  # fmt: skip
        return subprocess.CompletedProcess(cmd, 0, stream(data, "find_entities"), "")

    monkeypatch.setattr(bridge.subprocess, "run", fake)
    monkeypatch.setattr(bridge, "available", lambda: True)
    tools = [{"name": "find_entities", "description": "", "input_schema": {}}]
    res = bridge._turn("sys", ["hi"], tools, "sonnet", "medium", 60)
    assert res["text"] == "" and res["calls"] == [{"name": "find_entities", "arguments": {"query": "lamp"}}]
