"""Claude Code as the polygon's model backend (D30): every model call becomes one headless
`claude -p` turn with its own model and effort, so the whole polygon runs where only Claude Code is
logged in (claude.ai/code cloud sessions, a laptop) without OpenAI or Anthropic keys. Since D40 the
only model of the add-on is the orchestrator; the learner is no model.

    orchestrator  (Anthropic Messages API)              → `messages()`
    coordinator   (yes/no question)                     → `ask()`

Each call renders the conversation as text, lists the tools in the system prompt and asks for a
structured answer `{"text", "tool_calls": [{"name", "arguments"}]}` (`--json-schema`). No Claude
Code tools, settings, MCP servers or session files: the subprocess is only a model. Answers are
not the provider's native tool calling, so tool-call accuracy differs from the real backends; the
polygon finds bugs in the add-on, it does not benchmark the model.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import threading
import time
from typing import Any

MAX_PARALLEL = int(os.environ.get("AH_SIM_CLAUDE_PARALLEL", "4"))  # per process (one polygon)
_slots = threading.BoundedSemaphore(MAX_PARALLEL)
_ids = itertools.count(1)
PREAMBLE = (
    "You are the language model behind an HTTP API. The text below the line is the system prompt of "
    "the application calling you; follow it. Answer with exactly one assistant turn in the required "
    "JSON shape: `text` is what you say, `tool_calls` the tools you call now (empty when you are done "
    "or only answer). Arguments must match the tool's input schema. Never invent tools. The tools "
    "below are not functions you can call yourself: list them in `tool_calls`; the application runs "
    "them and sends the results in a later turn.\n"
)


# `--json-schema` answers through the CLI's own `StructuredOutput` tool. A `tool_use` of any other
# name is the model calling a tool natively; the CLI answers "No such tool available" and the model
# then writes about broken tools (stories 2026-11-15: the owner was told a call_service was
# broken before it was called). Mostly the tools are the application's own (find_entities,
# submit_automation, report): those calls are what the model meant, so they become the turn's
# `tool_calls` and the text about the failure is dropped. Any other native tool (Bash, Read …):
# the turn is asked again, then refused. Read from `--output-format stream-json`.
STRUCTURED = "StructuredOutput"
ATTEMPTS = 2


class BridgeError(Exception):
    pass


def available() -> bool:
    return shutil.which("claude") is not None


def _schema(names: list[str]) -> dict:
    call = {
        "type": "object",
        "properties": {
            "name": {"type": "string", **({"enum": names} if names else {})},
            "arguments": {"type": "object"},
        },
        "required": ["name", "arguments"],
    }
    calls: dict[str, Any] = {"type": "array", "items": call}
    if not names:
        calls["maxItems"] = 0
    return {
        "type": "object",
        "properties": {"text": {"type": "string"}, "tool_calls": calls},
        "required": ["text", "tool_calls"],
    }


def run(
    system: str, prompt: str, schema: dict, model: str, effort: str, timeout: float = 240.0,
    allowed: frozenset[str] = frozenset(),
) -> dict:
    """One `claude -p` turn → {"out": structured output, "model", "tokens_in", "tokens_out", "ms"}.
    `allowed`: the application's tools; native calls of these are taken as the turn's tool calls."""
    if not available():
        raise BridgeError("the `claude` CLI is not on PATH")
    cmd = ["claude", "-p", "--model", model, "--effort", effort, "--tools", "", "--no-session-persistence",
           "--setting-sources", "", "--strict-mcp-config", "--system-prompt", system,
           "--output-format", "stream-json", "--verbose", "--json-schema", json.dumps(schema)]  # fmt: skip
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}  # a nested session is fine here
    started, n_in, n_out = time.monotonic(), 0, 0
    for attempt in range(1, ATTEMPTS + 1):
        data, native = _once(cmd, prompt, env, timeout)
        foreign = {c["name"] for c in native} - allowed
        usage = data.get("usage") or {}  # every attempt was paid for
        n_in += sum(int(usage.get(k) or 0) for k in
                    ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))  # fmt: skip
        n_out += int(usage.get("output_tokens") or 0)
        if not foreign:
            break
        if attempt == ATTEMPTS:
            raise BridgeError(
                f"claude -p reached for native tools ({', '.join(sorted(foreign))}) {ATTEMPTS} times; "
                "its answer is not used"
            )
    out = data["structured_output"]
    if native:  # the application's tools, called natively: the calls are the answer
        out = {"text": "", "tool_calls": native}
    used = list((data.get("modelUsage") or {}).keys())
    return {
        "out": out,
        "model": used[0] if used else model,
        "tokens_in": n_in,
        "tokens_out": n_out,
        "ms": round((time.monotonic() - started) * 1000),
    }


def _once(cmd: list[str], prompt: str, env: dict, timeout: float) -> tuple[dict, list[dict]]:
    """One CLI run → (the final `result` event, the native tool calls of the model's first turn
    that made any: [{name, arguments}]; later turns only react to the CLI's "No such tool")."""
    with _slots:
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, timeout=timeout, env=env, check=False
            )
        except subprocess.TimeoutExpired as err:
            raise BridgeError(f"claude -p timed out after {timeout:g} s") from err
    data, native = None, []
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            data = event
        elif event.get("type") == "assistant":
            turn = (event.get("message") or {}).get("id") or len(native)
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("name") != STRUCTURED and (not native or native[-1]["turn"] == turn):
                    args = block.get("input") if isinstance(block.get("input"), dict) else {}
                    native.append({"name": str(block.get("name")), "arguments": args, "turn": turn})
    if data is None:
        raise BridgeError(f"claude -p: {(proc.stderr or proc.stdout).strip()[:300]}")
    if data.get("is_error") or not isinstance(data.get("structured_output"), dict):
        raise BridgeError(f"claude -p: {str(data.get('result') or data.get('subtype'))[:300]}")
    return data, [{"name": c["name"], "arguments": c["arguments"]} for c in native]


def _turn(system: str, turns: list[str], tools: list[dict], model: str, effort: str, timeout: float) -> dict:
    names = [t["name"] for t in tools]
    tool_text = json.dumps(tools, ensure_ascii=False) if tools else "none"
    full_system = f"{PREAMBLE}Tools you may call: {tool_text}\n----------\n{system}"
    prompt = "\n\n".join(turns) + "\n\n[Your next assistant turn as JSON.]"
    res = run(full_system, prompt, _schema(names), model, effort, timeout, allowed=frozenset(names))
    calls = [c for c in res["out"].get("tool_calls") or [] if isinstance(c, dict) and c.get("name") in names]
    return {**res, "text": str(res["out"].get("text") or ""), "calls": calls}


def _text(content: Any) -> str:
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return content or ""


def _tool(name: str, description: str | None, schema: dict | None) -> dict:
    return {"name": name, "description": description or "", "input_schema": schema or {}}


def _json(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


# ---- Anthropic Messages (orchestrator) --------------------------------------------------------------
def render_messages(body: dict) -> tuple[str, list[str], list[dict]]:
    system = body.get("system") or ""
    system = _text(system) if isinstance(system, list) else system
    turns = []
    for m in body.get("messages") or []:
        content = m.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        parts = []
        for b in blocks:
            if b.get("type") == "text" and b.get("text"):
                parts.append(b["text"])
            elif b.get("type") == "tool_use":
                parts.append(f"called {b.get('name')}({_json(b.get('input') or {})}) id={b.get('id')}")
            elif b.get("type") == "tool_result":
                err = " (error)" if b.get("is_error") else ""
                parts.append(f"TOOL RESULT id={b.get('tool_use_id')}{err}:\n{_text(b.get('content'))}")
        who = "ASSISTANT" if m.get("role") == "assistant" else "USER"
        turns.append(f"{who}:\n" + "\n".join(parts or ["…"]))
    tools = [_tool(t["name"], t.get("description"), t.get("input_schema")) for t in body.get("tools") or []
             if t.get("name")]  # fmt: skip
    return system, turns, tools


def messages(body: dict, model: str, effort: str, timeout: float = 240.0) -> dict:
    """Anthropic Messages request → Messages response body."""
    system, turns, tools = render_messages(body)
    r = _turn(system, turns, tools, model, effort, timeout)
    content: list[dict] = [{"type": "text", "text": r["text"]}] if r["text"] else []
    content += [
        {
            "type": "tool_use",
            "id": f"toolu_sim_{next(_ids)}",
            "name": c["name"],
            "input": c.get("arguments") or {},
        }
        for c in r["calls"]
    ]
    return {
        "id": f"msg_sim_{next(_ids)}",
        "type": "message",
        "role": "assistant",
        "model": r["model"],
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": "tool_use" if r["calls"] else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": r["tokens_in"], "output_tokens": r["tokens_out"]},
    }


# ---- yes/no (coordinator) ----------------------------------------------------------------------------
def ask(question: str, model: str = "haiku", effort: str = "low", timeout: float = 120.0) -> dict:
    schema = {
        "type": "object",
        "properties": {"same": {"type": "boolean"}, "why": {"type": "string"}},
        "required": ["same", "why"],
    }
    return run("Answer the question as JSON.", question, schema, model, effort, timeout)["out"]
