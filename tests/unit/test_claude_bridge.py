"""sim/claude_bridge: `claude -p` as the polygon's model (D30). The CLI is faked; no network."""

from __future__ import annotations

import json
import subprocess

import httpx
import pytest

from sim import claude_bridge as bridge


class FakeCLI:
    """Stands in for `subprocess.run(["claude", "-p", ...])`; records argv, prompt and schema."""

    def __init__(self, out: dict | None, **extra):
        self.out, self.extra, self.calls = out, extra, []

    def __call__(self, cmd, input, **kw):  # noqa: A002 - subprocess.run's own name
        self.calls.append({"cmd": cmd, "prompt": input, "env": kw.get("env") or {}})
        data = {"is_error": False, "structured_output": self.out, "usage": {"input_tokens": 10,
                "cache_read_input_tokens": 90, "output_tokens": 7},
                "modelUsage": {"claude-haiku-5-5": {}}, **self.extra}  # fmt: skip
        return subprocess.CompletedProcess(cmd, 0, json.dumps(data), "")

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


def test_room_chat_becomes_one_lean_claude_turn_with_tool_calls(cli):
    fake = cli({"text": "", "tool_calls": [{"name": "call_service", "arguments": {"entity_id": "light.k"}},
                                           {"name": "rm_rf", "arguments": {}}]})  # fmt: skip
    msg, finish, n_in, n_out = bridge.chat_completion(CHAT, "haiku", "low")
    assert finish == "tool_calls" and (n_in, n_out) == (100, 7)
    assert [c["function"]["name"] for c in msg["tool_calls"]] == ["call_service"]  # invented tools dropped
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"entity_id": "light.k"}
    assert msg["content"] is None
    # the subprocess is only a model: no tools, settings, MCP or sessions; model and effort as asked
    assert fake.flag("--model") == "haiku" and fake.flag("--effort") == "low" and fake.flag("--tools") == ""
    assert "--no-session-persistence" in fake.calls[-1]["cmd"] and "CLAUDECODE" not in fake.calls[-1]["env"]
    system, schema = fake.flag("--system-prompt"), json.loads(fake.flag("--json-schema"))
    assert "You run the kitchen." in system and '"call_service"' in system
    assert schema["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"] == ["call_service"]
    prompt = fake.calls[-1]["prompt"]
    assert "USER:\nmotion in the kitchen" in prompt and "called call_service(" in prompt
    assert "TOOL RESULT id=c1:\nshadow: logged" in prompt


def test_a_plain_answer_and_tool_choice_none(cli):
    fake = cli({"text": "nothing to do", "tool_calls": []})
    msg, finish, *_ = bridge.chat_completion({**CHAT, "tool_choice": "none"}, "haiku", "low")
    assert finish == "stop" and msg == {"role": "assistant", "content": "nothing to do"}
    assert json.loads(fake.flag("--json-schema"))["properties"]["tool_calls"]["maxItems"] == 0


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


def test_gateways_serve_room_and_orchestrator_through_the_bridge(cli, tmp_path):
    from anthropic import Anthropic

    from sim.fake_jetson import JetsonSim
    from sim.gateways import Gateways
    from sim.server import ServerThread
    from sim.sim_config import SimConfig

    cfg = SimConfig.load(tmp_path)
    cfg.update({"room": {"backend": "claude", "pace": False, "claude_model": "haiku"},
                "anthropic": {"backend": "claude", "claude_model": "opus", "claude_effort": "high"}})  # fmt: skip
    with pytest.raises(ValueError, match="Anthropic gateway only"):
        cfg.update({"openai": {"backend": "claude"}})
    gw = Gateways(cfg, JetsonSim(), {})
    llm, claude = ServerThread(gw.llm_app()), ServerThread(gw.cloud_app("anthropic"))
    llm.start()
    claude.start()
    try:
        fake = cli(
            {"text": "", "tool_calls": [{"name": "call_service", "arguments": {"entity_id": "light.k"}}]}
        )
        r = httpx.post(f"{llm.url}/v1/chat/completions", json=CHAT)  # no API key needed
        assert r.status_code == 200 and r.json()["choices"][0]["finish_reason"] == "tool_calls"
        assert fake.flag("--model") == "haiku"
        # the add-on's own SDK parses the answer, so the shape matches the Messages API
        fake.out = {"text": "ok", "tool_calls": []}
        resp = Anthropic(api_key="sk-ant-sim", base_url=claude.url, max_retries=0).messages.create(
            model="claude-opus-5-5", max_tokens=100, messages=[{"role": "user", "content": "hi"}]
        )
        assert resp.content[0].text == "ok" and resp.stop_reason == "end_turn"
        assert fake.flag("--model") == "opus" and fake.flag("--effort") == "high"
        assert [row["backend"] for row in gw.log.list()] == ["claude", "claude"]
    finally:
        llm.stop()
        claude.stop()
