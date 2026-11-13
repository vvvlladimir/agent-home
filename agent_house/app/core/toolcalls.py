"""Tool calls from small local models: extraction, content-embedded calls, conservative JSON repair.

Pure stdlib and Python 3.10 compatible on purpose: `jetson/bench/bench.py` imports this module on
the Jetson (JetPack 6 = Python 3.10), so the benchmark scores exactly what production would parse.

Repair is deliberately narrow. Arguments end up in service calls, so a guess is worse than an
error: we fix syntax (code fences, trailing commas, Python literals, text around the object,
missing closing brackets after a *complete* value) and never invent or complete a value. Output cut
off by the token limit (`finish_reason == "length"`) is never closed.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import Any

_THINK = re.compile(r"<think>.*?</think>", re.S)
_THINK_OPEN = re.compile(r"<think>.*\Z", re.S)
_TAGGED = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|\Z)", re.S)
_FUNCTION = re.compile(r"<function=([\w.-]+)>\s*(.*?)\s*(?:</function>|\Z)", re.S)
_PARAMETER = re.compile(r"<parameter=([\w.-]+)>\s*(.*?)\s*</parameter>", re.S)
_FENCED = re.compile(r"```[\w-]*\s*\n?(.*?)```", re.S)
_CLOSERS = {"{": "}", "[": "]"}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    raw: str = ""
    source: str = "native"  # native = message.tool_calls, content = parsed out of the text
    repaired: bool = False
    error: str | None = None  # set when the call cannot be used as is; arguments is {} then

    def as_message_entry(self) -> dict:
        """The call as it goes back into the conversation (always valid JSON arguments)."""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": json.dumps(self.arguments, ensure_ascii=False)},
        }


def _scan(text: str) -> tuple[str, list[str], bool, int | None]:
    """One string-aware pass: drop trailing commas, track open brackets.

    Returns (text without trailing commas, brackets still open, ended inside a string,
    end offset of the first complete top-level value or None).
    """
    out: list[str] = []
    stack: list[str] = []
    in_string = escaped = False
    end: int | None = None
    for char in text:
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            last = len(out) - 1
            while last >= 0 and out[last].isspace():
                last -= 1
            if last >= 0 and out[last] == ",":
                del out[last]
            if stack:
                stack.pop()
            if not stack and end is None:
                out.append(char)
                end = len(out)
                continue
        out.append(char)
    return "".join(out), stack, in_string, end


def _parse_candidate(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:  # Python literal: single quotes, True/False/None
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError) as err:
        raise ValueError(str(err)) from None
    if not isinstance(value, (dict, list)):
        raise ValueError("not an object")
    json.dumps(value)  # sets, bytes, … are not JSON
    return value


def loads_lenient(text: str, *, truncated: bool = False) -> tuple[Any, bool]:
    """`json.loads` with a few safe syntax repairs. Returns (value, repaired); raises ValueError."""
    if not isinstance(text, str):
        raise ValueError("not a string")
    stripped = text.strip()
    try:
        return json.loads(stripped), False
    except ValueError:
        pass
    fenced = _FENCED.search(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    starts = [i for i in (stripped.find("{"), stripped.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON object found")
    fixed, stack, in_string, end = _scan(stripped[min(starts) :])
    if end is not None:
        candidate = fixed[:end]
    elif not truncated and not in_string and stack and fixed.rstrip()[-1:] in ('"', "}", "]"):
        # every value is complete, only the closing brackets are missing
        candidate = fixed.rstrip() + "".join(_CLOSERS[b] for b in reversed(stack))
    else:
        raise ValueError("truncated JSON")
    try:
        return _parse_candidate(candidate), True
    except TypeError as err:
        raise ValueError(str(err)) from None


def parse_arguments(raw: Any, *, truncated: bool = False) -> tuple[dict, bool, str | None]:
    """Arguments of one call → (dict, repaired, error)."""
    if isinstance(raw, dict):
        return raw, False, None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}, False, None
    if not isinstance(raw, str):
        return {}, False, f"arguments have type {type(raw).__name__}, expected a JSON object"
    try:
        value, repaired = loads_lenient(raw, truncated=truncated)
        if isinstance(value, str):  # double-encoded: a JSON string that contains the object
            value, _ = loads_lenient(value, truncated=truncated)
            repaired = True
    except ValueError as err:
        return {}, False, f"arguments are not valid JSON ({err})"
    if not isinstance(value, dict):
        return {}, False, "arguments are not a JSON object"
    return value, repaired, None


def content_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content if isinstance(content, str) else ""


def strip_reasoning(text: str) -> str:
    return _THINK_OPEN.sub("", _THINK.sub("", text)).strip()


def visible_text(message: dict) -> str:
    """What the model said to the user: no reasoning block, no embedded tool calls."""
    text = strip_reasoning(content_text(message))
    return _FUNCTION.sub("", _TAGGED.sub("", text)).strip()


def _from_payload(payload: Any) -> list[tuple[str, Any]]:
    """`{"name": …, "arguments": …}` (or a list of them) → [(name, arguments)]."""
    if isinstance(payload, dict) and isinstance(payload.get("tool_calls"), list):
        payload = payload["tool_calls"]
    out = []
    for item in payload if isinstance(payload, list) else [payload]:
        if not isinstance(item, dict):
            continue
        item = item["function"] if isinstance(item.get("function"), dict) else item
        name = item.get("name") or (item.get("function") if isinstance(item.get("function"), str) else None)
        if isinstance(name, str) and name:
            args = item.get("arguments", item.get("parameters", {}))
            out.append((name, args))
    return out


def _embedded(text: str, known: set[str] | None, truncated: bool) -> list[tuple[str, Any, str, bool]]:
    """Tool calls written into the text → [(name, arguments, raw, repaired)]."""
    found: list[tuple[str, Any, str, bool]] = []
    tagged = _TAGGED.findall(text)
    for block in tagged:
        function = _FUNCTION.search(block)
        if function:  # <function=name><parameter=key>value</parameter></function>
            found.append(_function_block(function, truncated))
            continue
        try:
            payload, repaired = loads_lenient(block, truncated=truncated)
        except ValueError:
            found.append(("", None, block, False))
            continue
        found += [(name, args, block, repaired) for name, args in _from_payload(payload)]
    if tagged:
        return found
    for function in _FUNCTION.finditer(text):
        found.append(_function_block(function, truncated))
    if found or not known:
        return found
    # No markers: only trust plain JSON when it names a tool we offered.
    for block in [*_FENCED.findall(text), text]:
        try:
            payload, _ = loads_lenient(block, truncated=truncated)
        except ValueError:
            continue
        calls = [(name, args, block, True) for name, args in _from_payload(payload) if name in known]
        if calls:
            return calls
    return found


def _function_block(match: re.Match, truncated: bool) -> tuple[str, Any, str, bool]:
    name, body = match.group(1), match.group(2)
    parameters = _PARAMETER.findall(body)
    if parameters:
        args: dict = {}
        for key, value in parameters:
            try:
                args[key] = json.loads(value)
            except ValueError:
                args[key] = value
        return name, args, match.group(0), False
    if not body:
        return name, {}, match.group(0), False
    try:
        value, repaired = loads_lenient(body, truncated=truncated)
        return name, value, match.group(0), repaired
    except ValueError:
        return name, body, match.group(0), False


def extract_tool_calls(
    message: dict, finish_reason: str | None = None, known_tools: Any = None
) -> list[ToolCall]:
    """Every tool call in an assistant message, native or embedded in the text.

    Calls that cannot be used (broken arguments, unknown tool) are returned with `error` set so the
    caller can tell the model instead of silently dropping them.
    """
    known = set(known_tools) if known_tools else None
    truncated = finish_reason == "length"
    calls: list[ToolCall] = []
    native = message.get("tool_calls")
    if isinstance(native, list) and native:
        for index, entry in enumerate(native):
            entry = entry if isinstance(entry, dict) else {}
            function = entry.get("function") if isinstance(entry.get("function"), dict) else entry
            raw = function.get("arguments")
            args, repaired, error = parse_arguments(raw, truncated=truncated)
            calls.append(
                ToolCall(
                    id=str(entry.get("id") or f"call_{index}"),
                    name=str(function.get("name") or ""),
                    arguments=args,
                    raw=raw if isinstance(raw, str) else json.dumps(raw),
                    repaired=repaired,
                    error=error,
                )
            )
    else:
        text = strip_reasoning(content_text(message))
        for index, (name, raw, block, repaired) in enumerate(_embedded(text, known, truncated)):
            args, repaired_args, error = parse_arguments(raw, truncated=truncated)
            if not name:
                error = "tool call is not valid JSON"
            calls.append(
                ToolCall(
                    id=f"call_c{index}",
                    name=name,
                    arguments=args,
                    raw=block,
                    source="content",
                    repaired=repaired or repaired_args,
                    error=error,
                )
            )
    for call in calls:
        if call.error is None and (not call.name or (known is not None and call.name not in known)):
            call.error = f"unknown tool {call.name!r}"
            call.arguments = {}
    return calls
