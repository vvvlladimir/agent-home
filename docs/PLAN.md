# Plan

Checkboxes are the single source of progress. Done = simulator/CI checks pass **and** the hardware
steps are written down in `OWNER_CHECKLIST.md`.

## Phase 0 — Foundations

- [x] Research → `RESEARCH.md` (HA apps, ingress, HA API, llama.cpp/Jetson, Qwen3.5, Anthropic API)
- [x] `DECISIONS.md`, `OWNER_CHECKLIST.md`, this plan
- [x] `.claude/agents/*.md`, `CLAUDE.md`, `Makefile`
- [x] `contracts/`: OpenAPI for the whole GUI API (with `x-milestone`), JSON Schemas for agent config,
      policy, Jetson metrics, bench report, tool-call eval, setup check, diagnostics manifest
- [x] `sim/`: fake HA (REST + WS), fake llama-server, fake Anthropic, fake Jetson metrics, fake ingress
- [x] CI: lint, contracts, tests (3.13 + 3.14), app linter, hadolint, image build amd64 + aarch64 with
      a container smoke test, HA client against real HA Core

## M1 — Skeleton installs

- [x] Add-on packaging: `config.yaml`, `Dockerfile`, `run.sh`, translations, DOCS, CHANGELOG, icon/logo
- [x] App starts against `sim/`, GUI served under a simulated ingress prefix, relative URLs enforced by tests
- [x] Read-only HA client: auth, reconnect with backoff, registries, area→entity mapping, live states
- [x] API: status, mode, kill switch, areas, entities, feed + SSE, settings, logs, diagnostics bundle
- [x] GUI: dashboard, live feed, entity browser with tiers, settings & logs; placeholders for later pages
- [x] Tier resolution (`core/policy.py`) with invariant tests
- [x] CI green on GitHub (run 2: lint+tests 3.13/3.14, app linter, hadolint, amd64+aarch64 image, real HA Core)
- [x] **Owner:** install on the Pi, open the panel, commit screenshot + diagnostics (OWNER_CHECKLIST M1, 2026-11-13)
- [x] **Owner:** answers Q1–Q7 → `owner-reports/answers.md`

## M2 — Inference stack

- [ ] `jetson/setup.sh` (`--check`, `--dry-run`, `--json`), `build_llamacpp.sh`, `profiles/*.env`,
      `llama-server.service`, `models.md`; shellcheck clean
- [ ] `jetson/metrics/server.py`: tegrastats → `/metrics.json`, `POST /profile/<name>` with token
- [ ] `jetson/bench/bench.py`, `run_all.sh`, `toolcall_eval.yaml` (≥ 30 scenarios), validated against `sim/fake_llm.py`
- [ ] `core/llm.py`: priority queue, coalescing, stale drop, health, warming state, retry, metrics
- [ ] Tool-call JSON repair + content-embedded tool-call parsing
- [ ] API + GUI: Models & Benchmarks page; `PUT api/settings`
- [ ] Gemma 4 E2B comparison profile
- [ ] **Owner:** `setup.sh --check`, setup, `bench/run_all.sh`; then `make analyze-report` → final default profile

## M3 — Room agents in Shadow Mode

- [ ] Policy decision function: kill switch → allowlist → service allowlist → tier → rate limit → mode
- [ ] Single actuation path (`Actuator`), T2 approvals with expiry, action log
- [ ] Event gate: per-agent rules, debounce, cooldown, hourly cap; cron wakes
- [ ] Agent runtime: compact context, stable prompt prefix, step/token/time limits, escalate instead of guess
- [ ] Tools: get_state, list_entities, get_history, call_service, report, ask_agent, schedule_wake, remember/recall
- [ ] Traces: store, API, GUI page with replay and copy-as-test-case
- [ ] Agents CRUD page (area picker, tiers, wake rules, prompt preview with token estimate)
- [ ] Kill switch from HA (`agent_house_kill_switch` event, optional `input_boolean`)
- [ ] `tests/eval`: tool-call eval against scripted LLM; two sample rooms (owner, Q3: Bedroom, Livingroom)
- [ ] If SSE does not pass the real ingress: polling fallback for the feed

## M4 — Orchestrator + automation lifecycle

- [ ] Providers: anthropic / local / auto with fallback; daily budget with hard stop
      (owner, Q6: local Qwen is the default once M2 passes; cloud fallback `claude-haiku-5-5`)
- [ ] Redaction for cloud prompts
- [ ] Lifecycle: propose → YAML/schema/policy scan/`validate_config` → diff → approve → deploy → version → rollback
- [ ] HA client write paths guarded by the `agent_house_` prefix
- [ ] Shadow evidence: trigger simulation against history
- [ ] Proposals + Deployed automations pages
- [ ] Align `sim/fake_ha.py` `validate_config` with real HA (plural keys); history includes the state at start
- [ ] Safety tests: T3 never actuated, drafts never reach HA, kill switch halts everything

## M5 — Migration + polish

- [ ] Importer (HA automations/scripts, Node-RED export — samples in `owner-reports/nodered/`), parity report, reversible "disable original"
- [ ] Chat page, config export/import
- [ ] RUNBOOK, README screenshots, `v0.1.0`
