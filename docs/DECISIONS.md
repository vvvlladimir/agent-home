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
| D14 | Orchestrator providers: `auto` = local first, Claude when the local model fails or is down; `local`; `anthropic`. Cloud spend is stored per day (`spend` table); the worst case of a call (prompt estimate + `max_tokens`, plus a fallback model) is reserved before it and settled after it, so concurrent or failed calls cannot overspend; no SDK retries; a fallback answer is priced at both models; unknown models are priced like the most expensive one. Default model `claude-haiku-5-5`, effort `medium`, strict tools, no forced `tool_choice`; refusal fallbacks only on models that have them. | Owner Q6 + D11; the budget must hold even when a call fails half-way. |
| D15 | The orchestrator only drafts: its single output is a proposal through the lifecycle, traced as a run of agent `orchestrator`. Room agents reach it through `propose_automation` (once per run, never on replays, at most 3 per agent and day). | No second path to HA; the owner sees what the model was asked and what it answered. |

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
| S9 | `scene` and `input_boolean` are T2 by default (were T1). | Both act indirectly: a scene can set any entity, a helper can trigger the owner's automations, so their own tier says nothing about what they touch (M3 review). The owner can lower them in `policy.yaml`. |
| S10 | An agent's automatic scope drops device noise (`update`, `device_tracker`, battery/signal/voltage/connectivity sensors, `_linkquality`/`_rssi`/`_last_seen`/… ids; `button` stays), ranks low tiers first (T0 → T3), then room sensors (motion, temperature, …), then other sensors. The first `max_entities` go into the prompt; the rest (up to 200) stay in the allowlist and are found with `list_entities(search=…)`. | A real room has 100+ entities, mostly bookkeeping; a small model gets lost in a long list, but must still reach everything in its room. Tiers apply unchanged. Ranking by use in the owner's automations is not built (M5 importer reads them). |
| S11 | Prompt order: wake-rule entities, then entities the agent read or used (last 30 days, `entity_usage`), then available actuators by tier, room sensors, other sensors; unavailable entities and indicator LEDs (`*_rgb_light`, `*_status_led`, …) last. A quarter of `max_entities` is kept for room sensors. | Owner run r_1a1d4212c23abec23: 20 slots went to unavailable devices and sensor-box LEDs, no sensor was visible, the agent looped. Usage makes the prompt follow what the agent actually needs. |
| S12 | Repeating a read (`get_state`, `list_entities`, `get_history`, `recall`) is allowed, with a "finish now" note from the second time; a repeated identical action is refused once, then the run escalates. `schedule_wake` once per run; neither it nor `ask_agent` on a wake by another agent or by a schedule. | States change, so re-reading is legitimate; actions and agent-to-agent calls are what looped (09:37 ping-pong in the owner's runs). The step limit still bounds every run. |
| S13 | T3 entities of settings domains (`number`, `select`, `text`, `button`, `time`, `date`, `datetime`) rank after the room's sensors; T3 devices with a real state (lock, alarm, camera) keep their tier rank. The default prompt says occupancy changes alone are not worth a report and to describe only states that were read. | Owner run r_1a1d43953a55cf263: six `number.*_off_delay`/`*_countdown` settings sat ahead of the sensors, the model reported a `warning` because someone walked in and described zone states it had not read (owner-reports/m3/README.md). |
| S14 | Automation lifecycle: every create/edit/approve/rollback re-runs yaml → schema → policy → HA `validate_config`. The policy scan accepts only literal `domain.service` calls allowed by the policy (plus `notify.*`, `persistent_notification.create`) on explicitly named, known entities of that domain; blocks T3, the kill-switch helper, `event:` steps, scripts, `automation.*`/`homeassistant.*`, area/device/floor/label targets, templated targets and webhook triggers; recurses into choose/if/sequence/parallel/repeat. Approving a proposal is the owner's approval for its T2 entities. The kill switch blocks approve, rollback and enable; disable and remove stay possible. A config id that exists in HA but was not deployed by us is never overwritten. | Drafts must be harmless until a human says yes, and a deployed automation runs without per-action approval, so the scan is stricter than the runtime policy. |
| S15 | Cloud redaction: `person.*`, `device_tracker.*`, `zone.*` ids become aliases (restored in the answer); the home's, people's and zones' names, e-mails, URLs, IP/MAC addresses, phone numbers, coordinates and keys/tokens become placeholders. Any other entity id that contains a word of such a name (`light.anna_bedroom`) is aliased too. Other entity ids and their states stay (the model needs them). | "Cloud calls carry redacted summaries only"; deny-leaning patterns, verified by a test that greps the fake Anthropic's requests. |
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
### Benchmarks

| file | source | profile | prefill tok/s | decode tok/s | TTFT warm | wake warm | tool calls | min free RAM | meets targets |
|---|---|---|---|---|---|---|---|---|---|
| 2026-11-13_gemma4-e2b-compare.json | jetson | gemma4-e2b-compare | 2454.0 | 29.0 | 200 ms | 19430 ms | 25/36 | — | no: warm wake 19430 ms (target ≤ 8000); tool-call accuracy 69% (target ≥ 95%); minimum free RAM — (target ≥ 1024) |
| 2026-11-13_qwen35-2b-fast.json | jetson | qwen35-2b-fast | 1256.4 | 32.8 | 498 ms | 2427 ms | 20/36 | — | no: tool-call accuracy 56% (target ≥ 95%); minimum free RAM — (target ≥ 1024) |
| 2026-11-13_qwen35-4b-balanced-pp0.json | jetson | qwen35-4b-balanced@pp0 | 475.4 | 16.2 | 1082 ms | 6009 ms | 27/36 | — | no: tool-call accuracy 75% (target ≥ 95%); minimum free RAM — (target ≥ 1024) |
| 2026-11-13_qwen35-4b-balanced.json | jetson | qwen35-4b-balanced | 514.7 | 15.9 | 891 ms | 6753 ms | 26/36 | — | no: tool-call accuracy 72% (target ≥ 95%); minimum free RAM — (target ≥ 1024) |

**Recommendation:** no measured profile meets all targets; keep the provisional default and see the reasons in the table.

### Jetson check — 2026-11-13_check.json

- module: NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super (Orin Nano 8 GB: True)
- L4T 36.5.0, JetPack major 6, CUDA 12.6.11
- power mode: MAXN_SUPER; RAM available 5039 MB of 7607 MB; swap 11995 MB
- WARN: token — no token yet: the profile switch and llama-server would be open or refused
- WARN: unit:llama-server.service — not installed or out of date
- WARN: unit:agent-house-metrics.service — not installed or out of date
- WARN: sudoers — the metrics service cannot restart llama-server yet

### Diagnostics — agent-house-diagnostics-20261113-005800.zip

- app 0.1.0-dev.1 on aarch64 / Python 3.14.10
- HA connected: True (version 2026.11.1, via Supervisor: True, 5 areas, 1887 entities)
- ingress path seen: /api/hassio_ingress/<43 chars>; peer 172.30.32.2; rejected peers 0

### Diagnostics — agent-house-diagnostics-20261113-080957.zip

- app 0.1.0-dev.2 on aarch64 / Python 3.14.10
- HA connected: True (version 2026.11.1, via Supervisor: True, 5 areas, 1888 entities)
- ingress path seen: /api/hassio_ingress/<43 chars>; peer 172.30.32.2; rejected peers 0

<!-- analyze-report:end -->
