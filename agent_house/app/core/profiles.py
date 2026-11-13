"""Model profiles: what the add-on needs to know about each model the Jetson can serve.

The server side of a profile (GGUF, context, KV cache type) lives in `jetson/profiles/<name>.env`;
this catalogue holds the request side (sampling, output limit). `tests/unit/test_jetson_files.py`
keeps both in step. Pure stdlib and Python 3.10 compatible (shared with `jetson/bench/bench.py`).
"""

from __future__ import annotations

import re

DEFAULT_PROFILE = "qwen35-4b-balanced"

# Qwen3.5 non-thinking sampling per the vendor (docs/RESEARCH.md §5); Gemma 4 per Google's defaults.
_QWEN = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.5}
_GEMMA = {"temperature": 1.0, "top_p": 0.95, "top_k": 64, "presence_penalty": 0.0}

PROFILES: list[dict] = [
    {
        "name": "qwen35-4b-balanced",
        "model": "unsloth/Qwen3.5-4B-GGUF:Q4_K_M",
        "ctx_size": 8192,
        "max_output_tokens": 512,
        **_QWEN,
        "thinking": False,
        "provisional": True,
        "experimental": False,
        "notes": "Provisional default: the only candidate with a third-party Orin Nano measurement.",
    },
    {
        "name": "qwen35-2b-fast",
        "model": "unsloth/Qwen3.5-2B-GGUF:Q4_K_M",
        "ctx_size": 8192,
        "max_output_tokens": 512,
        **_QWEN,
        "thinking": False,
        "provisional": True,
        "experimental": False,
        "notes": "Fallback if the 4B misses the latency target.",
    },
    {
        "name": "gemma4-e2b-compare",
        "model": "unsloth/gemma-4-E2B-it-GGUF:Q4_K_M",
        "ctx_size": 8192,
        "max_output_tokens": 512,
        **_GEMMA,
        "thinking": False,
        "provisional": True,
        "experimental": False,
        "notes": "Comparison candidate with native function calling; no Jetson numbers published.",
    },
    {
        "name": "qwen35-9b-experimental",
        "model": "unsloth/Qwen3.5-9B-GGUF:Q4_K_M",
        "ctx_size": 4096,
        "max_output_tokens": 512,
        **_QWEN,
        "thinking": False,
        "provisional": True,
        "experimental": True,
        "notes": "Experiment: fills the 8 GB; expect little headroom and possibly swap.",
    },
]

_VARIANT = re.compile(r"^(?P<base>[a-z0-9][a-z0-9._-]*)@pp(?P<pp>\d+(?:\.\d+)?)$")
SAMPLING_KEYS = ("temperature", "top_p", "top_k", "presence_penalty")


def get_profile(name: str | None) -> dict | None:
    """Profile by name. `<name>@pp<value>` is the same profile with another presence penalty
    (the benchmark compares the vendor's 1.5 against 0 for tool-call JSON)."""
    if not name:
        return None
    variant = _VARIANT.match(name)
    base = variant.group("base") if variant else name
    for profile in PROFILES:
        if profile["name"] == base:
            if variant:
                return {**profile, "name": name, "presence_penalty": float(variant.group("pp"))}
            return dict(profile)
    return None


def sampling(profile: dict | None, presence_penalty: float | None = None) -> dict:
    """Request parameters for a profile; unknown profile = the default profile's."""
    profile = profile or get_profile(DEFAULT_PROFILE) or {}
    params = {key: profile[key] for key in SAMPLING_KEYS if key in profile}
    if presence_penalty is not None:
        params["presence_penalty"] = presence_penalty
    return params
