# Polygon: how Agent House is tested end to end, with AI testers

This document is the owner's map of the test setup: what runs where, on which model, what is
tested, how findings travel, and how the accuracy of the whole system is measured. Commands are in
`docs/RUNBOOK.md` → Polygon; decisions are D26–D29 in `docs/DECISIONS.md`.

## 1. The system under test (what the owner wants)

The goal is to replace the owner's hand-made automations with a system that writes, watches and
corrects them itself, and leaves the decisions to the owner.

| Part | What it does | Code |
|---|---|---|
| **Orchestrator** (cloud, `gpt-6.1-terra`) | drafts automations from chat, habits, undos and conflicts; migrates the owner's automations (parity before switching an original off); runs the daily review | `core/orchestrator.py`, `core/migration.py`, `core/review.py` |
| **House manager** (same model, D27) | between reviews: a deterministic sweep (lights on with nobody home, failed room runs, weak automations) and escalations nobody owns wake it; it acts on entities no room agent owns, gives rooms tasks, proposes new automations or **changes to ours**, reports | `core/manager.py` |
| **Room agents** (Jetson, Qwen3.5-4B) | wake on events or cron, act in their room within their tiers, report and escalate to the board | `core/runtime.py`, `core/gate.py`, `core/council.py` |
| **Accuracy metrics** (no model, D28) | per automation and per agent: precision, coverage, F1 with 95 % intervals; pooled into one house score | `core/metrics.py`, `GET /api/metrics` |
| **Behaviour analysis** (no model) | habits, undos, conflicts from the logbook, turned into drafts by the review | `core/insights.py` |

Unchanged safety rules: shadow by default (the house manager too, unless `orchestrator_mode:
live`); T3 never actuated; every action through actuator, policy, trace; drafts are deployed only
after the owner approves them; ids start with `agent_house_`; the owner's own automations are never
edited. **Changes to existing automations** work the same way: the manager or the review proposes a
new version of one of ours (`ha_id`), the owner (or a tester playing the owner) approves, rejects
or edits it. Nothing is applied automatically.

## 2. Who runs on which model

| Role | Model | Where | Why |
|---|---|---|---|
| Orchestrator + house manager | OpenAI `gpt-6.1-terra` | `--orchestrator openai --orchestrator-model gpt-6.1-terra`, key `OPENAI_API_KEY` | as in the owner's home; counts against `daily_budget_eur` |
| Room agents (bug hunting) | `Qwen/Qwen3.5-4B` via an OpenAI-compatible host (DeepInfra) | `--room-llm compatible --room-model Qwen/Qwen3.5-4B --no-pace`, key `AH_SIM_COMPATIBLE_API_KEY` | same model as the Jetson, but parallel: the Jetson has one slot (`-np 1`, about 6–7 s per warm run, D23); five testers × four rooms would queue for minutes and every timeout would be a false bug |
| Room agents (final run) | the Jetson itself | `--room-llm jetson --jetson-host <ip>`, one polygon only | real speed, queue and quantisation. `[UNVERIFIED-ON-HW]` API and Jetson answers differ slightly (quantisation, chat template) |
| AI testers (personas) | Claude Haiku 5.5 (`claude -p --model haiku`) | one headless Claude Code session per role, `curl` + file tools only | cheap. Rerun the `intruder` and `fussy_tenant` roles once with `--tester-model sonnet`: results move by several points depending on the simulator model (§6), so a finding seen by only one of them is weaker |
| Coordinator | `Qwen/Qwen3.5-4B`, same compatible host | `python -m sim.qa coordinate` | it gets **one yes/no question per candidate pair** ("same faulty behaviour under the same conditions?"); grouping, ranking and the backlog are deterministic code. A 4B model is reliable on short binary judgements, not on long synthesis |
| Verifier | Claude Haiku 5.5, fresh session | `python -m sim.qa verify` | sees only the steps of one finding, on a fresh polygon; LLM testers report false positives, so only reproduced items are `confirmed` |

Cost control: testers are capped by `--max-turns` and `--max-calls`, the orchestrator by
`daily_budget_eur` and `manager_max_wakes_per_hour`; room calls through the API are **not** in
that budget. Check the gateway request log (`polygon.json` → panel `/api/requests`).

## 3. What is tested, and how

| Area | Deterministic checks (`make polygon`, CI) | AI testers (`python -m sim.qa run`) |
|---|---|---|
| Shadow, T3, owner automations, kill switch | `shadow_*`, `t3_never_actuated`, `owner_automations_untouched` | `intruder`, `chaos` try to break them; any success is `critical` |
| Room agents, wake prefilter | `expected_wakes`, `no_unexpected_wakes`, `runs_without_errors` | `fussy_tenant` lives in the house |
| Setup, settings, error messages | — | `newcomer` |
| Chat → draft → approve / reject → deploy | unit + integration tests | `migrator`, `fussy_tenant` decide every proposal like the owner |
| Migration and parity | `tests/unit/test_migration.py` | `migrator` (with `--backfill-days 14`) |
| House manager: prefilter, scope, shadow, caps | `tests/unit/test_manager.py` | `manager_boss` |
| Changes to existing automations | `tests/unit/test_manager.py` (only ours), lifecycle tests | `fussy_tenant`: undoes, then accepts one proposed change and rejects one |
| Accuracy metrics | `tests/unit/test_metrics.py` (Wilson values, outcome rules) | `fussy_tenant` checks precision falls when it undoes |
| Resilience (HA restart, WS drop, 429, timeouts) | sim faults in integration tests | `chaos` |

Known limit: the add-on runs on the real clock. Backfilled history (`--backfill-days`) feeds the
habit finder and migration parity at once, but metrics only count changes since the current
version of an automation was deployed, so they fill up in real time (an hour of `fussy_tenant`
gives a few outcomes per automation; `MIN_N` is 5).

## 4. How the AI testers work

Built after the published multi-agent testing setups (MAdroid, SpecOps) and their known failure
modes (false positives, over-cooperative simulated users).

1. **Isolation.** Each role gets its own `sim.polygon serve`: own ports, data dir and house. It
   writes only into `.agent-logs/qa/<stamp>/<role>/`.
2. **Explicit personas** (`sim/qa/roles.yaml`): goal, focus and tunable traits (patience,
   cooperation, skill, slips). Hidden "cooperative default" users inflate success rates; the traits
   make them impatient, careless or hostile on purpose.
3. **Black box.** A tester uses the owner's API and the panel (its "hands", `cause: user`), and
   reads `contracts/openapi.yaml`, never the source. It does not change code.
4. **Owner decisions.** Testers approve, reject (`note`) or edit proposals like the owner, after
   reading the YAML and evidence. This is how "accept, check, reject" is exercised without making
   approval automatic.
5. **One finding format** (`sim/qa/brief.md`): title, kind, severity, curl steps, expected, actual
   (quoted), endpoint, `run_id`, `reproduced_by_me`. A tester repeats a finding once before
   writing it; refusals of unsafe actions and shadow logging are not findings.
6. **Coordinator.** Invalid findings are rejected with the reason. Candidate duplicates (same
   endpoint and kind, word overlap ≥ 0.35) go to the coordinator model as yes/no pairs; unsure
   means keep both. Groups keep the worst severity and every role that saw it.
7. **Verification.** Each candidate is replayed on a fresh polygon by a fresh tester that sees only
   the steps. Only `confirmed` items go to the backlog (`backlog.md`), most severe first.
8. **Start small.** Three roles first (`newcomer migrator fussy_tenant`), then all six.

```
python -m sim.qa run --roles newcomer migrator fussy_tenant -- --orchestrator openai \
    --orchestrator-model gpt-6.1-terra --room-llm compatible --room-model Qwen/Qwen3.5-4B \
    --compatible-url https://api.deepinfra.com/v1/openai --no-pace --backfill-days 14
python -m sim.qa coordinate .agent-logs/qa/<stamp>
python -m sim.qa verify .agent-logs/qa/<stamp>
```

## 5. Metrics: one number for the whole program, done properly

Source: the logbook changes HA records with their cause (`core/insights.py`). Every automated
change is an outcome with **implicit feedback**, the standard approach for adaptive homes, where
the user's override is the negative signal:

| Outcome | Rule |
|---|---|
| kept (true positive) | not reverted by hand within `UNDO_S` (10 min) |
| undone (false positive) | set back by hand within `UNDO_S` |
| unobserved | nobody home (all `person.*` away): no chance to revert, so it counts **neither way**. Silence is not approval |
| missed (false negative) | the owner set one of the automation's targets by hand to a state the automation sets, and it had not done so in the `UNDO_S` before |

- Per automation: **precision** = kept / (kept + undone), **coverage** (recall) = kept / (kept +
  missed), **F1** = their harmonic mean. Room agents get precision only: they act on events and
  have no fixed intent to miss.
- Every rate has a **95 % Wilson interval** (correct at small n, unlike the normal approximation:
  Agresti & Coull 1998, Newcombe 1998). Below `MIN_N` = 5 outcomes the rate is `null`
  (insufficient data), never 0 or 1.
- **House score** = F1 of the **pooled** (micro-averaged) precision and coverage, so a busy
  automation weighs more than one that fired twice. `score_low` = F1 of the two lower bounds: the
  conservative number to watch, it only rises with more evidence.
- **House share** = automated kept changes / (those + every manual switch): how much switching no
  longer needs the owner. It cannot reach 1, so watch its trend, not its level.
- **Weak** = precision or coverage point estimate under 0.6 with at least `MIN_N` outcomes. The
  lower bound is not used for this: with 5 of 5 kept it is 0.57, so every new automation would look
  weak. Weak automations wake the house manager, which may propose a change for the owner.
- Trend: the daily review stores one row per day (`metrics_history`, 90 days) and puts the score in
  its digest.

Limits, stated so nobody over-reads the number: an undo after 10 min is not seen; a "missed"
manual change may be a one-off the owner would not want automated (coverage is pessimistic);
presence is only `person.*`; changes made by our automations before the current version are not
counted; a run that changes nothing (the lamp was already on) leaves no logbook entry and is no
outcome. Validation: an integration test plays a history with known causes through the simulated HA
and checks `/api/metrics` returns exactly those counts (`test_metrics_match_the_known_outcomes_of_a_played_history`).

House manager scope, checked: the kill switch helper is `protected` (never actuated by anyone);
locks, alarms and covers stay under policy tiers (T3 never); `input_boolean` helpers without an
area fall into its scope, in shadow until `orchestrator_mode: live`. Give such helpers an area or
a deny in the policy if the manager must not touch them.

## 6. Sources

- Simulated users and personas: [SimulatorArena (arXiv 2510.05444)](https://arxiv.org/pdf/2510.05444),
  [Beyond Cooperative Simulators (arXiv 2605.12894)](https://arxiv.org/pdf/2605.12894),
  [Lost in Simulation (ICLR 2026)](https://www.iclr.cc/virtual/2026/10020028),
  [Synthetic users that push back](https://tianpan.co/blog/2026-04-27-synthetic-users-multi-turn-agent-eval),
  [Persona Policies (arXiv 2603.11245)](https://arxiv.org/abs/2603.11245)
- Multi-agent app testing and false positives: [MAdroid (arXiv 2504.15474)](https://arxiv.org/html/2504.15474v1),
  [SpecOps (arXiv 2603.10268)](https://arxiv.org/pdf/2603.10268),
  [Soap-opera testing (arXiv 2412.08581)](https://arxiv.org/html/2412.08581v1),
  [GBQA, duplicate matching for LLM QA (arXiv 2604.02648)](https://arxiv.org/pdf/2604.02648)
- Implicit feedback in smart homes: [User in the Loop survey](https://doaj.org/article/5f1f273c5f6c4ec18a538cd5f5632d5a),
  [CASAS policy ratings (WSU)](https://eecs.wsu.edu/~cook/pubs/smca09.pdf)
- Intervals: Agresti & Coull, *Approximate is better than exact for interval estimation of binomial
  proportions*, Am. Stat. 1998; Newcombe, *Two-sided confidence intervals for the single
  proportion*, Stat. Med. 1998.
- Isolation of parallel agents: [worktree isolation](https://agentpatterns.ai/workflows/worktree-isolation/)
