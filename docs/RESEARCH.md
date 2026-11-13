# Research (Phase 0, November 2026)

Short findings with sources. Web content was treated as data. Confidence tags:

- **[VERIFIED-LOCAL]** reproduced in the cloud sandbox (real HA Core, real base image).
- **[DOC]** stated by current official docs or source code.
- **[UNVERIFIED]** could not be confirmed from a fetched source; also listed in `OWNER_CHECKLIST.md`.
- **[UNVERIFIED-ON-HW]** can only be confirmed on the owner's Pi / Jetson / HA.

## 1. Home Assistant add-on ("app") packaging

Docs moved: `developers.home-assistant.io/docs/add-ons/*` redirects to
<https://contributors.home-assistant.io/apps/>; add-ons are now called "apps".

- **config.yaml** [DOC] <https://contributors.home-assistant.io/apps/reference/configuration/>
  - Required: `name`, `version` (quoted), `slug`, `description`, `arch`.
  - `arch`: `aarch64`, `amd64`. `armhf`/`armv7`/`i386` still load but warn (deprecated).
  - `init: false` for the s6-overlay base images. `startup: application`, `boot: auto`.
  - Ingress: `ingress: true`, `ingress_port` (default 8099), `ingress_stream`, `panel_icon`,
    `panel_title`, `panel_admin`.
  - API: `homeassistant_api: true` (Core API through the Supervisor proxy), `hassio_api`, `hassio_role`.
  - `map`: list of dicts `- type: share` with `read_only` (default true). Types include
    `homeassistant_config`, `app_config`, `share`, `media`, `ssl`, `backup`, `data`.
  - `options` + `schema`; types `str`, `bool`, `int`, `float`, `password`, `url`, `port`,
    `list(a|b)`, `match(regex)`; `?` suffix = optional.
    <https://contributors.home-assistant.io/apps/guides/options/>
- **BUILD_FROM / build.yaml** [DOC] Since Supervisor 2026.04.0 `BUILD_FROM` is no longer passed and
  `build.yaml` is deprecated. Name the base image in the Dockerfile and pin a tag (the Dockerfile
  guide recommends a version tag over `latest`).
  <https://contributors.home-assistant.io/blog/2026/04/02/builder-migration>,
  <https://contributors.home-assistant.io/apps/guides/dockerfile/>,
  <https://github.com/home-assistant/supervisor/releases/tag/2026.04.0>
- **Base images** [DOC] multi-arch: `ghcr.io/home-assistant/base` (`3.24`),
  `ghcr.io/home-assistant/base-python` (`3.14-alpine3.24`), `base-debian`, `base-ubuntu`.
  There is no current Python 3.12 base image, so the add-on runs on **Python 3.14**.
  - [VERIFIED-LOCAL] `base-python:3.14-alpine3.24` pulls and runs our app on amd64 (Python 3.14.10).
  - [VERIFIED-LOCAL] every dependency has a cp314 musllinux wheel for **aarch64 and x86_64**
    (`pip download --platform musllinux_1_2_aarch64 --only-binary=:all:`), so the image needs no compiler.
  - [VERIFIED-LOCAL] **the base image's `/init` (s6-overlay) does not pass the container
    environment to `CMD`.** A plain `#!/bin/sh` start script sees an empty `SUPERVISOR_TOKEN`;
    `#!/usr/bin/with-contenv sh` fixes it. `tools/addon_lint.py` and the CI smoke test guard this.
- **Labels** [DOC] `io.hass.type=app`, `io.hass.arch`, `io.hass.version` (Supervisor adds them for local builds).
- **Translations** [DOC] `translations/en.yaml` → `configuration: <option>: {name, description}`.
- **CI** [DOC] the legacy `home-assistant/builder` action is deprecated; we use plain
  `docker/build-push-action` on native amd64 and arm64 runners. Linter: `frenck/action-addon-linter@v2`
  (docs now call it the app linter). [VERIFIED-LOCAL: CI] `@v2` resolves; it accepts our `schema`
  (including `float(0,100)` and `password?`) and **rejects keys that restate a default** (`boot`,
  `startup`, `ingress_port: 8099`, `panel_admin`, `hassio_api: false`).
- [VERIFIED-LOCAL: CI] the unmodified image builds on a native arm64 runner and on amd64, and the
  container starts and answers (`.github/workflows/ci.yaml`, job "Build image").
- [UNVERIFIED] `repository.yaml` field list (we use `name`, `url`, `maintainer`), icon/logo sizes
  (we ship 128×128 and 250×100).

## 2. Ingress

[DOC] <https://contributors.home-assistant.io/apps/guides/ingress/>

- Only accept connections from **172.30.32.2** (Supervisor proxy).
- Every request carries `X-Ingress-Path` (the prefix the app is served under).
- Use **relative URLs** everywhere (`static/app.js`, not `/static/app.js`).
- HTTP/1.x, streaming and WebSockets pass through; default port 8099.
- [UNVERIFIED-ON-HW] Server-Sent Events through the real ingress proxy (the live feed uses SSE;
  works through `sim/fake_ingress.py`). If it buffers, the GUI falls back to polling in M3.

## 3. Home Assistant API from inside the add-on

[DOC] <https://contributors.home-assistant.io/apps/supervisor-api/>,
<https://contributors.home-assistant.io/core/internals/rest-api/>,
<https://contributors.home-assistant.io/core/internals/websocket-api/>

- Token: env `SUPERVISOR_TOKEN`. REST proxy `http://supervisor/core/api/...` (needs
  `homeassistant_api: true`), WebSocket `ws://supervisor/core/websocket`, auth message
  `{"type": "auth", "access_token": "<SUPERVISOR_TOKEN>"}`. [UNVERIFIED-ON-HW] exercised only against
  `sim/fake_ha.py`, which serves the same paths.
- REST: `GET /api/states`, `POST /api/services/<domain>/<service>` (returns changed states),
  `GET /api/history/period/<ts>?filter_entity_id=…&end_time=…`, `POST /api/config/core/check_config`.
- WebSocket: `auth_required` → `auth` → `auth_ok`; `subscribe_events`, `get_states`, `get_config`,
  registry lists.

Shapes confirmed against a real, headless **Home Assistant Core 2026.2.3** (the newest release that
installs on the sandbox's Python 3.13) and, in CI, against the current release on Python 3.14.
`tests/integration/test_real_ha.py`, 8 tests, all passing: **[VERIFIED-LOCAL]**

- `config/area_registry/create|list` → `area_id`, `name`; `config/entity_registry/list` entries have
  `entity_id`, `area_id`, `device_id`, `disabled_by`, `hidden_by`, `name`, `original_name`;
  `config/entity_registry/update` returns `{"entity_entry": …}`; `config/device_registry/list` works.
- `state_changed` events carry `entity_id`, `old_state`, `new_state`.
- Automation entities expose their config id as `attributes.id` and have `last_triggered`.
- `POST /api/config/automation/config/<id>` → `{"result": "ok"}`, **reloads by itself** (the entity
  appears without calling `automation.reload`); `GET` returns the config with `id`; unknown keys →
  HTTP 400 with `message`; `DELETE` → `{"result": "ok"}`, then `GET` is 404.
- `automation.turn_off` exists and sets the entity to `off`.
- WebSocket `validate_config` takes **plural** keys (`triggers`, `conditions`, `actions`) and answers
  `{"triggers": {"valid": bool, "error": …}}`. `sim/fake_ha.py` follows this since M4 (singular keys are an error).
- A wrong bearer token → HTTP 401.
- Headless start without onboarding: pre-seed `.storage/auth` + `.storage/onboarding` and sign a
  long-lived JWT ourselves (`tests/integration/real_ha.py`). The recorder rejects in-memory SQLite.

## 4. llama.cpp on the Jetson Orin Nano 8 GB

- **Build** JetPack 6.x (R36.4.x, Ubuntu 22.04, CUDA 12.6, GCC 11.4):
  `cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 -DLLAMA_CURL=ON` then
  `cmake --build build --config Release -j2` (`-j2`: CUDA template compiles can peak above 4 GB RSS).
  Remove a stale `build/` first, or the wrong arch is silently kept.
  <https://github.com/claytonvlawson/jetson-orin-nano-models>,
  <https://forums.developer.nvidia.com/t/compile-llama-cpp-to-use-the-jetson-orin-nano-super-gpu/343758>
- **JetPack 7.2** supports Orin (Ubuntu 24.04, kernel 6.8, CUDA 13.x); same cmake flags; anything built
  against CUDA 12.6 must be rebuilt. <https://developer.nvidia.com/embedded/jetpack/downloads>,
  <https://amintechs.com/tutorials/orin-nano-super-jetpack-7-2-llamacpp-systemd?lang=en>
- **llama-server flags** [DOC] <https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md>:
  `--jinja` is now the default; `-ngl N|auto|all`; `-fa on|off|auto`; `-ctk/-ctv` (f16 default; q8_0,
  q4_0 … allowed; no "turbo" types in mainline); `-np N`; `-c N`; `--cache-reuse N`;
  `--reasoning-budget N` (0 = off); `--chat-template-kwargs '{"enable_thinking":false}'`;
  `--metrics`; `--api-key`; `-hf <repo>[:quant]`; `/health` is 503 while loading, then 200.
  - [UNVERIFIED] default port, exact `/metrics` names, `--mlock` / `--no-mmap` spelling in current
    builds, and the `timings` field names (`prompt_n`, `prompt_per_second`, `predicted_per_second`,
    `cache_n`). Scripts set `--port` explicitly and `bench.py` reads `timings` defensively;
    `setup.sh --check` records `llama-server --help` so the flags can be checked against the build.
- **Power mode** [UNVERIFIED-ON-HW] sources disagree on the MAXN_SUPER mode id (0 vs 2). Never hardcode:
  parse `nvpmodel -p --verbose`. Use `ExecStart=-/usr/sbin/nvpmodel …` in systemd so a no-op does not
  skip `jetson_clocks`.
- **Detection** [UNVERIFIED-ON-HW] `/proc/device-tree/model`, `/etc/nv_tegra_release`,
  `dpkg-query --show nvidia-l4t-core`.
- **Memory** default swap is small; an NVMe swapfile is recommended for 7B+ models; dropping the page
  cache before a large load helps. [UNVERIFIED] zram-disable and headless (`multi-user.target`) steps.
- **tegrastats** sample (RidgeRun): `RAM 5933/7620MB (lfb 21x1MB) SWAP 5/3810MB (cached 0MB) CPU […]
  … cpu@…C gpu@…C tj@…C … VDD_IN 5313mW/5313mW`, GPU load as `GR3D_FREQ X%`. [UNVERIFIED-ON-HW] exact
  line on the owner's JetPack: the metrics parser treats every field as optional and returns the raw line.
  <https://docs.nvidia.com/jetson/l4t/Tegra%20Linux%20Driver%20Package%20Development%20Guide/AppendixTegraStats.html>

## 5. Models

- **Qwen3.5 0.8B / 2B / 4B / 9B** exist (released 2 March 2026, Apache-2.0, thinking and non-thinking
  modes). GGUF: `unsloth/Qwen3.5-{0.8B,2B,4B,9B}-GGUF`. 9B file confirmed as
  `Qwen3.5-9B-UD-Q4_K_XL.gguf`; [UNVERIFIED] the 4B file name. <https://unsloth.ai/docs/models/qwen3.5>
- **Sampling, non-thinking**: temperature 0.7, top_p 0.8, top_k 20, min_p 0, presence_penalty 1.5.
- **Thinking is off by default** on 0.8B–9B; keep it off (`{"enable_thinking": false}` makes it explicit).
- **Tool calling**: improved after Unsloth's template fixes (5 March); no independent benchmark of the
  4B found. Our own `toolcall_eval.yaml` on the owner's Jetson is the evidence that counts.
- **Measured on an Orin Nano Super** (third party, MAXN_SUPER, R36.4.3, `-ngl 99`, ctx 16384, flash-attn,
  q8_0 KV): a Qwen3.5-4B fine-tune at **~16 tok/s, peak RSS 3.9 GB, cold load 21 s**; 8–9B class at
  ~10 tok/s and 5.4–5.9 GB. <https://github.com/claytonvlawson/jetson-orin-nano-models>
- **9B with a TurboQuant KV fork**: a forum post reports Qwen3.5-9B Q4 with ~100k context on the Orin
  Nano Super; fork-specific, months old, not in mainline. Opt-in experiment only.
  <https://forums.developer.nvidia.com/t/weekend-home-lab-qwen3-5-9b-on-jetson-orin-nano-super-with-turboquant4-100k-token-window/366306>
- **MTP / speculative decoding**: Seeed reports gains on AGX Orin 32 GB, with a user comment about
  quality loss on Qwen3.5. Not a default. <https://wiki.seeedstudio.com/jetson/accelerate_llm_inference_mtp/>
- **Alternatives for 8 GB**: no authoritative ranking; published tool-calling numbers are vendor claims
  on differing benchmark versions. Gemma 4 E2B (native function calling, no Jetson numbers yet) is the
  one worth benchmarking as a second candidate. <https://huggingface.co/blog/nvidia/gemma4>
- [UNVERIFIED] Ollama mmproj/vision caveats: nothing confirmed; we use plain `llama-server` anyway.

## 6. Anthropic API (orchestrator, M4)

From the Claude API reference bundled with Claude Code (model table cached 2026-11-03).

- Official Python SDK `anthropic` 2.x (`AsyncAnthropic`), Messages API, manual tool loop (we need a
  policy check, budget check and trace around every step).
- Default model **`claude-haiku-5-5`** since D11 (owner Q6; $0.10 / $0.50 per MTok); `claude-opus-5-5` is $4 / $20. Cheaper options the owner may choose:
  `claude-sonnet-5-5` ($2 / $10), `claude-haiku-5-5` ($0.10 / $0.50). The model is an add-on option.
- Request rules on current models: no `temperature` / `top_p` / `top_k`; thinking cannot be disabled
  and takes no budget (omit `thinking`, steer with `output_config.effort`; default `medium` on Opus 5.5,
  set it explicitly); **no forced `tool_choice`** (`any` / `tool` → 400; use `auto` plus `strict: true`
  tools); no assistant prefill. `sim/fake_anthropic.py` rejects all of these so tests catch regressions.
- Always check `stop_reason` (`tool_use`, `end_turn`, `max_tokens`, `refusal`). Opt in to server-side
  refusal fallbacks (`betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`).
- Prompt caching: prefix match over `tools` → `system` → `messages`; keep the system prompt and tool
  list byte-stable and put volatile house state last; verify with `usage.cache_read_input_tokens`.
- Typed errors (`RateLimitError`, `APIStatusError`, `APIConnectionError`) drive the `auto` fallback to
  the local model.

- SDK 2.x sends the API key as `Authorization: Bearer …` (not `x-api-key`); `sim/fake_anthropic.py`
  accepts both. [VERIFIED-LOCAL, anthropic 2.8.0]
