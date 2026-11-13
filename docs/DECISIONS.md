# Decisions

Newest first within each section. "Provisional" = based on published numbers, not on the owner's
hardware; replaced after `make analyze-report` has real data.

## Architecture

| # | Decision | Why |
|---|---|---|
| D1 | The add-on (Pi 5) talks to a standalone `llama-server` on the Jetson over HTTP. | The add-on cannot run on the Jetson; HA must survive the Jetson being off. |
| D2 | Backend: Python + FastAPI + uvicorn, one process, SQLite (WAL) in `/data`. | Small, async, easy to debug; no extra services on a Pi. |
| D3 | **Python 3.14 at runtime**, 3.12-compatible syntax. | The only current HA Python base image is `base-python:3.14-alpine3.24`; a 3.12 image no longer exists (RESEARCH §1). CI tests 3.13 and 3.14. |
| D4 | GUI: **no-build static SPA** with vendored Alpine.js, JSON API, SSE for the live feed. | No Node toolchain in the image, works offline, every URL relative for ingress. The API contract decouples GUI work from backend work. Server-rendered templates would tie each page to its route handler. |
| D5 | i18n: the English text is the key (`t('Kill switch')`); `static/i18n/<lang>.json` maps it. | Complete scaffolding without a string catalogue to maintain; de/ru are one JSON file each. |
| D6 | The Supervisor builds the image locally from the Dockerfile (no `image:` key). | Simplest install for a prototype; wheels only, so the build on a Pi is quick. Prebuilt GHCR images can come with v0.1.0. |
| D7 | Start script uses `#!/usr/bin/with-contenv sh`, no bashio; options are read from `/data/options.json` by the app. | s6 does not pass the environment to `CMD` (verified); the same config code runs in tests. |
| D8 | CI builds with `docker/build-push-action` on native amd64 and arm64 runners. | `home-assistant/builder` is deprecated; a native arm64 runner is the closest thing to the Pi's CPU without hardware. |
| D9 | Orchestrator: official `anthropic` SDK, manual tool loop, default model `claude-opus-5-5`, configurable. | We need policy, budget and trace hooks around every step. Model choice and cost are the owner's call (RESEARCH §6). |
| D10 | Model swapping on the Jetson goes through our small metrics/control endpoint (restart the systemd unit with another profile), not llama-server router mode. | One resident model is the rule on 8 GB; a restart is explicit, visible ("warming") and works on any llama.cpp build. |
| D11 | Default provider: local Qwen on the Jetson once M2 benchmarks pass; cloud is the fallback with `claude-haiku-5-5`. Until then, `claude-haiku-5-5`. Supersedes the `claude-opus-5-5` default in D9. | Owner's answer to Q6 (2026-11-13): Haiku is enough, local first. |
| D12 | The add-on's LLM manager owns the single llama-server slot: priority queue (interactive > agent > background), coalescing by key, stale drop, bounded wait while the model loads, retry only for 5xx / transport / unparsable. | `-np 1` on 8 GB: requests must queue in the add-on, where priorities and staleness are known, not on the Jetson. A timeout is not retried: the slot is busy. |
| D13 | The GUI benchmark and `jetson/bench/bench.py` share `app/core/benchkit.py` and `toolcall_eval.yaml`. Hardware reports come from the Jetson; the GUI run is a quick check of the live setup. | One scoring logic; the GUI cannot restart llama-server between profiles for a cold-load measurement. |

## Safety

| # | Decision | Why |
|---|---|---|
| S1 | Unknown domains are T3. Sensor-like domains are T3 too and shown as "read-only". | Deny by default; nothing to actuate there anyway. |
| S2 | A T3 domain can only be lifted by `t3_whitelist` (exact entity ids), and only to T2. | "Never unless the owner explicitly whitelists", and even then a human approves each action. |
| S3 | Agent tier overrides can only tighten. | A prompt or config mistake in one agent must not widen its rights. |
| S4 | Garage covers match `cover.*garage*` → T3 by default. | Covers are T2, but a garage door is a perimeter. Entities named differently need an explicit `entity_tiers` entry (OWNER_CHECKLIST Q4). |
| S5 | The app rejects every peer except 172.30.32.2; no published port; `/healthz` is the only loopback exception. | The GUI can switch modes; it must only be reachable through HA's authenticated ingress. |
| S6 | The M1 HA client is read-only. Service calls and automation writes arrive with the policy engine (M3) and lifecycle (M4), each as the single code path for its kind. | Nothing can actuate by accident before the safety layer exists. |
| S7 | Kill switch also blocks deploys. Already deployed automations keep running in HA. | "Reflexes stay deterministic"; panic-disabling them is a separate, explicit action (M4). |
| S8 | Diagnostics and settings output mask `ha_token`, `anthropic_api_key`, `jetson_token`; a test greps the bundle for the raw values. | The bundle is meant to be committed to the repo. |

## Models (provisional until the owner's benchmark)

| Profile | Model | Settings | Status |
|---|---|---|---|
| `qwen35-4b-balanced` | Qwen3.5-4B, Q4_K_M-class quant (`unsloth/Qwen3.5-4B-GGUF`) | ctx 8192, q8_0 KV, `-ngl 99`, flash-attn, `-np 1`, thinking off, temp 0.7 / top_p 0.8 / top_k 20 / presence 1.5 | **provisional default** |
| `qwen35-2b-fast` | Qwen3.5-2B | ctx 8192–16384, same flags | fallback if 4B misses the latency target |
| `gemma4-e2b-compare` | Gemma 4 E2B-it, Q4_K_M (`unsloth/gemma-4-E2B-it-GGUF`) | ctx 8192, same flags, template default, temp 1.0 / top_p 0.95 / top_k 64 / presence 0 | comparison candidate |
| `qwen35-9b-experimental` | Qwen3.5-9B Q4 | only resident model; mainline `q8_0` KV first, TurboQuant fork only as an opt-in experiment | experiment |

- The 4B is the only candidate with a third-party Orin Nano measurement (~16 tok/s, 3.9 GB peak;
  RESEARCH §5), which leaves the ≥ 1 GB headroom the memory rule asks for. Not measured by us.
- One resident model. A second one only if the benchmark shows ≥ 1 GB free and no swap use.
- Newer families: nothing found that is clearly better for tool calling on 8 GB. Gemma 4 E2B gets a
  profile in M2 so the owner's benchmark can compare it; Qwen3.5-4B stays the default until then.
- Presence penalty 1.5 is the vendor recommendation for chat; whether it hurts JSON tool arguments is
  an open question for the eval (M2 runs the eval at 1.5 and 0).

## Could not be done in the cloud

- Docker build inside the sandbox cannot reach PyPI through the TLS-inspecting proxy; verified instead
  by installing the pre-downloaded wheels into the real base image. CI does the unmodified build.
- A real HA Core runs headless here (2026.2.3 on Python 3.13); CI runs the same tests on the current
  release. The Supervisor, ingress and the `supervisor/core` proxy cannot be run: see OWNER_CHECKLIST.

## Measured results (generated by `make analyze-report`)

<!-- analyze-report:begin -->
### Diagnostics — agent-house-diagnostics-20261113-005800.zip

- app 0.1.0-dev.1 on aarch64 / Python 3.14.10
- HA connected: True (version 2026.11.1, via Supervisor: True, 5 areas, 1887 entities)
- ingress path seen: /api/hassio_ingress/<43 chars>; peer 172.30.32.2; rejected peers 0

<!-- analyze-report:end -->
