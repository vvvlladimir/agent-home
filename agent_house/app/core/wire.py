"""OpenAI-compatible chat completion wire format as llama-server speaks it.

Pure stdlib and Python 3.10 compatible (shared with `jetson/bench/bench.py`, see toolcalls.py).
Field names of `timings` are [UNVERIFIED] against the owner's build (docs/RESEARCH.md §4), so
everything here is read defensively: a missing field is None, never an exception.
"""

from __future__ import annotations

import json
from typing import Any


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def read_timings(timings: Any, usage: Any = None) -> dict:
    """Normalise llama-server `timings` (+ `usage` as a fallback for the token counts)."""
    timings = timings if isinstance(timings, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    prompt_n = _number(timings.get("prompt_n"))
    prompt_ms = _number(timings.get("prompt_ms"))
    predicted_n = _number(timings.get("predicted_n"))
    predicted_ms = _number(timings.get("predicted_ms"))
    prefill = _number(timings.get("prompt_per_second"))
    decode = _number(timings.get("predicted_per_second"))
    if prefill is None and prompt_n and prompt_ms:
        prefill = prompt_n / prompt_ms * 1000
    if decode is None and predicted_n and predicted_ms:
        decode = predicted_n / predicted_ms * 1000
    return {
        "prompt_n": prompt_n,  # tokens actually processed (cached ones excluded)
        "prompt_ms": prompt_ms,
        "prefill_tps": prefill,
        "predicted_n": predicted_n if predicted_n is not None else _number(usage.get("completion_tokens")),
        "predicted_ms": predicted_ms,
        "decode_tps": decode,
        "cache_n": _number(timings.get("cache_n")),
        "prompt_tokens": _number(usage.get("prompt_tokens")),
    }


def parse_completion(payload: Any) -> dict:
    """A non-streaming response → {message, finish_reason, usage, timings, model}. Raises ValueError."""
    try:
        choice = payload["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("response has no choices[0].message") from None
    if not isinstance(message, dict):
        raise ValueError("choices[0].message is not an object")
    return {
        "message": message,
        "finish_reason": choice.get("finish_reason"),
        "usage": payload.get("usage"),
        "timings": payload.get("timings"),
        "model": payload.get("model"),
    }


class StreamAccumulator:
    """Feed it SSE lines of a streaming chat completion; ask for the assembled message at the end."""

    def __init__(self) -> None:
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.calls: dict[int, dict] = {}
        self.finish_reason: str | None = None
        self.usage: Any = None
        self.timings: Any = None
        self.model: str | None = None
        self.error: str | None = None
        self.chunks = 0
        self.done = False

    def feed_line(self, line: str) -> bool:
        """Returns True when the line carried generated output (text, reasoning or a tool call)."""
        line = line.strip()
        if not line.startswith("data:"):
            return False
        data = line[5:].strip()
        if data == "[DONE]":
            self.done = True
            return False
        try:
            chunk = json.loads(data)
        except ValueError:
            return False
        if not isinstance(chunk, dict):
            return False
        self.chunks += 1
        if chunk.get("error"):
            error = chunk["error"]
            self.error = str(error.get("message") if isinstance(error, dict) else error)
            return False
        self.model = chunk.get("model") or self.model
        self.usage = chunk.get("usage") or self.usage
        self.timings = chunk.get("timings") or self.timings
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return False
        choice = choices[0]
        self.finish_reason = choice.get("finish_reason") or self.finish_reason
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        produced = False
        if delta.get("content"):
            self.content.append(str(delta["content"]))
            produced = True
        if delta.get("reasoning_content"):
            self.reasoning.append(str(delta["reasoning_content"]))
            produced = True
        for part in delta.get("tool_calls") or []:
            if not isinstance(part, dict):
                continue
            index = part.get("index") if isinstance(part.get("index"), int) else len(self.calls)
            call = self.calls.setdefault(index, {"id": None, "name": "", "arguments": ""})
            function = part.get("function") if isinstance(part.get("function"), dict) else {}
            call["id"] = part.get("id") or call["id"]
            call["name"] += function.get("name") or ""
            arguments = function.get("arguments")
            call["arguments"] += arguments if isinstance(arguments, str) else ""
            produced = True
        return produced

    def message(self) -> dict:
        message: dict = {"role": "assistant", "content": "".join(self.content) or None}
        if self.calls:
            message["tool_calls"] = [
                {
                    "id": call["id"] or f"call_{index}",
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call["arguments"]},
                }
                for index, call in sorted(self.calls.items())
            ]
        return message

    def result(self) -> dict:
        return {
            "message": self.message(),
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "timings": self.timings,
            "model": self.model,
        }
