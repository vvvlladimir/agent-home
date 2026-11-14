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

- [x] `jetson/setup.sh` (`--check`, `--dry-run`, `--json`), `build_llamacpp.sh`, `profiles/*.env`,
      `llama-server.service`, `models.md`; shellcheck clean
- [x] `jetson/metrics/server.py`: tegrastats → `/metrics.json`, `POST /profile/<name>` with token
- [x] `jetson/bench/bench.py`, `run_all.sh`, `toolcall_eval.yaml` (≥ 30 scenarios), validated against `sim/fake_llm.py`
- [x] `core/llm.py`: priority queue, coalescing, stale drop, health, warming state, retry, metrics
- [x] Tool-call JSON repair + content-embedded tool-call parsing
- [x] API + GUI: Models & Benchmarks page; `PUT api/settings`
- [x] Gemma 4 E2B comparison profile
- [ ] **Owner:** `setup.sh --check`, setup, `bench/run_all.sh`; then `make analyze-report` → final default profile

## M3 — Room agents in Shadow Mode

- [x] Policy decision function: kill switch → allowlist → service allowlist → tier → rate limit → mode
- [x] Single actuation path (`Actuator`), T2 approvals with expiry, action log
- [x] Event gate: per-agent rules, debounce, cooldown, hourly cap; cron wakes
- [x] Agent runtime: compact context, stable prompt prefix, step/token/time limits, escalate instead of guess
- [x] Tools: get_state, list_entities, get_history, call_service, report, ask_agent, schedule_wake, remember/recall
- [x] Traces: store, API, GUI page with replay and copy-as-test-case
- [x] Agents CRUD page (area picker, tiers, wake rules, prompt preview with token estimate)
- [x] Kill switch from HA (`agent_house_kill_switch` event, optional `input_boolean`)
- [x] `tests/eval`: tool-call eval against scripted LLM; two sample rooms (owner, Q3: Bedroom, Livingroom)
- [x] Polling fallback for the feed (SSE passed ingress in M1; fallback kicks in after 8 s without a stream)
- [x] **Owner:** two room agents in Shadow, traces + case files (2026-11-13, ~1 h, 12 runs, no actions;
      findings fixed: settings ranked after sensors (S13), report noise, case category, Jetson metrics
      answering errors → `owner-reports/m3/README.md`). The full day in Shadow continues alongside M4.

## M4 — Orchestrator + automation lifecycle

- [x] Providers: anthropic / local / auto with fallback; daily budget with hard stop
      (owner, Q6: local Qwen is the default once M2 passes; cloud fallback `claude-haiku-5-5`) → `core/orchestrator.py`, D14
- [x] Redaction for cloud prompts → `core/redact.py`, S15
- [x] Lifecycle: propose → YAML/schema/policy scan/`validate_config` → diff → approve → deploy → version → rollback → `core/lifecycle.py`, S14
- [x] HA client write paths guarded by the `agent_house_` prefix (`guard_ours`)
- [x] Shadow evidence: trigger simulation against history → `core/evidence.py`
- [x] Proposals + Deployed automations pages; room agents suggest automations (`propose_automation`)
- [x] Align `sim/fake_ha.py` `validate_config` with real HA (plural keys); history includes the state at start
- [x] Safety tests: T3 never actuated, drafts never reach HA, kill switch halts everything (`tests/unit/test_safety_invariants.py`, `test_lifecycle.py`, `tests/integration/test_m4_automations.py`)
- [ ] **Owner:** draft, check, simulate, deploy, roll back one automation on the real HA (OWNER_CHECKLIST M4)

## M5 — Migration + polish

- [x] Importer (HA automations/scripts, Node-RED export — samples in `owner-reports/nodered/`), parity report, reversible "disable original"
      → `core/migration.py`, `core/nodered.py`, D18, S16
- [x] Chat page, config export/import → `api/chat.py`, `Orchestrator.chat`, D19
- [x] Habit finder: deterministic scan of HA history (no LLM) for repeated manual actions that follow
      a sensor or a time of day (e.g. desk lamp switched on by hand when radar zone 1 turns on);
      only candidates with enough repeats go to the orchestrator, which drafts a proposal with evidence
      → `core/insights.py`, `Review.draft`, findings on the Proposals page, D20
- [x] GUI redesign: six tabs, overview with decisions / rooms / today, tooltips instead of help text, Vue 3 → `agent_house/frontend/`, D24, D25
- [x] Daily house review: the orchestrator once a day reads agent reports, the inbox and a history
      summary; proposes new automations, fixes for weak ones, and settles conflicts between rooms
      (one proposal per conflict, never two automations fighting over one entity)
      → `core/review.py` (one proposal per entity), proposal `supersedes` in `core/lifecycle.py`, D22, S17
- [x] Feedback after deploy: count how often the owner undoes what an `agent_house_` automation did
      (switches the light back within minutes); above a threshold, propose a fix or a rollback
      → `core/insights.py` (`find_undos`, rollback suggested above 60 %), `Review`, D20
- [x] Agent council — ownership: every entity has exactly one owner agent; room entities belong to
      their room agent, everything without an area (people, phones, weather, sun, house-wide helpers)
      to the orchestrator; no agent actuates or automates an entity it does not own
      → `Ownership` in `core/council.py`, policy step 2b, lifecycle check, D22, S17
- [x] Agent council — shared board: agents post short facts ("bedroom: someone went to bed 23:10",
      "living room: empty 30 min, TV on") with an expiry; every wake sees its neighbours' fresh facts
      in the prompt; replaces free agent-to-agent chat (no loops, bounded tokens)
      → `Board` in `core/council.py`, `Council.prompt_lines`, Council page, D22
- [x] Agent council — house modes: the orchestrator owns `input_select.agent_house_mode`
      (night / away / home / guests / work); room agents and deployed automations read it, so the
      instant reactions between rooms run in HA, not through a model
      → `Council.evaluate_mode` / `set_mode`, `HAClient.create_mode_helper`, D22, S17
- [x] Agent council — tasks down, escalation up: the orchestrator gives a room a task ("away: check
      everything is off") and gets a report on the board; a room escalates to the orchestrator (not to
      a neighbour) when it needs another owner's entity, sees a conflict or danger; `ask_agent` goes
      through the orchestrator → `Council.ask` / `Council.escalate` (forwarded to the owner, bounded), D22
- [x] Orchestrator as an agent: its own wake rules (mode changes, presence of people, daily review),
      routine runs on the Jetson, the cloud only as the expert for drafts, history analysis,
      conflicts and low-confidence cases (provider `auto` per task); 4B profile recommended for the
      room agents (`docs/DECISIONS.md` after the owner's benchmark)
      → `Council.status().orchestrator_wakes`, `Orchestrator.providers(task)`, D21, D23
- [x] RUNBOOK, README screenshots, `v0.1.0` → `docs/RUNBOOK.md`, `docs/img/v010-*.png`, tag `v0.1.0`
- [ ] **Owner:** council, daily review and feedback on the real HA (OWNER_CHECKLIST M5 council, v0.1.0)

## Sim panel — a home to test against without hardware

- [x] House generator: Zigbee2MQTT + router + HA housekeeping + junk, presets, deterministic seed,
      rebuild like an integration reload → `sim/house_gen.py`
- [x] Life: streaming sensors, rate / pause / storm, the owner's state-trigger automations,
      backfilled history and logbook, scenarios → `sim/life.py`, `sim/scenarios.py`
- [x] Model gateways: room agents on fake / real Jetson by IP / OpenAI-compatible / OpenAI / Claude
      with Jetson pacing and context limit; orchestrator Claude and OpenAI fake or real; keys only in
      the sim → `sim/gateways.py`, `sim/sim_config.py`, D26
- [x] Sim panel on :8090 (no build, localhost only) → `sim/control.py`, `sim/panel/`, RUNBOOK → Simulator
- [x] Polygon: headless scenario runs on free ports with a fresh house per scenario, scenario
      speed-up, invariant checks, JSON report and exit code; real backends by flag with a preflight
      → `sim/polygon.py`, `sim/polygon.yaml`, `tests/integration/test_polygon.py`, RUNBOOK → Polygon
- [ ] **Owner:** point the panel at the real Jetson and at the cloud APIs once (OWNER_CHECKLIST → Sim panel)
- [ ] **Owner:** one `make polygon ARGS="--room-llm openai"` (or `jetson`) run with a real key
