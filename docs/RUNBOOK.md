# Runbook

How to run Agent House day to day and what to do when something goes wrong. Install steps and
options: [`agent_house/DOCS.md`](../agent_house/DOCS.md). Hardware setup and the owner's test steps:
[`OWNER_CHECKLIST.md`](OWNER_CHECKLIST.md). Why things are the way they are:
[`DECISIONS.md`](DECISIONS.md).

## The pieces

| Piece | Runs on | If it is down |
|---|---|---|
| Home Assistant + deployed `agent_house_*` automations | Pi 5 (HA OS) | nothing works; HA is the source of truth |
| Agent House add-on (GUI, agents, policy, orchestrator) | Pi 5, Supervisor add-on | deployed automations keep running in HA; no agent wakes |
| `llama-server` + metrics endpoint | Jetson Orin Nano (`llama-server.service`, `agent-house-metrics.service`) | room agents cannot run; with `orchestrator_provider: auto` and a key the orchestrator uses the cloud |
| Claude / OpenAI API (optional) | cloud | drafts stay local; spend stops at `daily_budget_eur` |

State lives in `/data` of the add-on: `agent_house.db` (SQLite: runs, traces, actions, proposals,
automation versions, board, findings, spend), `settings.yaml` (GUI settings), the agents and
`policy.yaml`. A Home Assistant backup that includes the add-on saves all of it.

## Safety levers, from gentle to hard

1. **Agent to shadow** — Agents → the agent → mode *shadow*. It keeps waking and logging what it
   would do.
2. **Global mode to Shadow** — top bar. Every agent is in shadow, whatever its own mode says.
3. **Kill switch** — top bar, or from HA: fire the event `agent_house_kill_switch` (or switch the
   `input_boolean` named in `kill_switch_entity` on). No service call, no deploy, no draft, no
   review leaves the add-on. Already deployed automations keep running in HA (S7).
4. **Disable one of our automations** — Automations → Deployed → untick *On* (or roll back to an
   earlier version).
5. **Stop the add-on** — Settings → Add-ons → Agent House → Stop. HA runs on, with only its own
   automations and the `agent_house_*` ones.

Releasing the kill switch does not switch anything back on by itself: agents act again only on
their next wake, and only where policy and mode allow.

## Going from Shadow to Live (per agent)

Do this one room at a time, after a few days of traces that look right.

1. Activity → Runs → filter the agent: read the last ~20 traces. Every `shadow:` action should be one you
   would have wanted. Copy wrong ones *as a test case* and report them.
2. Check the agent's tiers under Rooms (open the agent): T0 acts silently, T1 acts and notifies, T2 waits for
   your approval on the Overview, T3 never acts.
3. Set the global mode to **Live**, then the agent's mode to **live**. Other agents stay in shadow.
4. Watch the live feed for an hour. Anything odd: back to shadow (lever 1).

The orchestrator itself actuates exactly one entity, `input_select.agent_house_mode`, and only when
`auto_house_mode` is on and `orchestrator_mode` is `live` (Settings). It never leaves `guests` or
`work`, which are yours.

## Daily routine (two minutes)

- **Inbox** (dashboard): T2 approvals waiting, reports with severity `warning` / `escalate`, and the
  orchestrator's *Daily review* (default 04:30, `daily_review_at`).
- **Automations → Drafts**: drafts from the review, from room agents and from *Found in the history*. Each has
  four checks (yaml, schema, policy, `ha_validate`); a `blocked` one cannot be approved. Simulate
  against history before approving. A proposal that `supersedes` another automation of ours
  switches that one off when it is deployed (you can switch it back on).
- **Council**: house mode, the shared board (what the rooms told each other), who owns what.

## Incidents

| Symptom | Look at | Do |
|---|---|---|
| A light or device switches on its own | Activity → Live feed (agent and run id) → Runs → trace; Automations → Deployed | Shadow the agent, or disable the automation; report the trace |
| An automation of ours keeps getting undone | Automations → Drafts → *Found in the history* (`undo` findings); the review suggests a fix, above 60 % a rollback | Approve the fix or roll back under Automations → Deployed |
| Two automations switch one light back and forth | *Found in the history* → `conflict` finding | Approve the conflict proposal: it replaces one of ours and supersedes the other; switch your own one off from Migration if it is one of yours |
| Agents never wake | Connection status in the header (hover it), Settings → Models, Rooms → agent → Wakes, feed lines `gate:` | Check the Jetson (below); check the hourly wake cap |
| Models page says *warming* for minutes | Jetson: `journalctl -u llama-server -n 100` | A profile switch loads a model (≈ 10–60 s); if it loops, switch back to `qwen35-4b-balanced` |
| Jetson unreachable | `curl http://<jetson>:9100/metrics.json`, `systemctl status llama-server agent-house-metrics` | `sudo systemctl restart llama-server`; check power mode `MAXN_SUPER` and free RAM |
| Cloud drafts stopped | Connection status in the header (hover it), Settings → General → Add-on options | Budget reached: wait for the next day or raise `daily_budget_eur`; 401: check the key |
| Kill switch engaged and you did not do it | Feed line naming the source (GUI / HA event / helper) | Find who fired it (HA logbook) before releasing |
| GUI does not load | Add-on log; open it only through the HA sidebar (direct ports are refused, S5) | Restart the add-on; send a diagnostics bundle |

## Upgrades and rollback of the add-on

- Read `agent_house/CHANGELOG.md`, then Settings → Add-ons → Agent House → Update. The database
  migrates itself on start; there is no downgrade path for it, so take an HA backup first.
- To return to an earlier add-on version, restore that backup (partial backup of the add-on).
- Settings → **Config export** saves agents, policy and GUI settings as YAML (no secrets); importing
  it puts every agent into shadow.

## Leaving

Stop or uninstall the add-on. The `agent_house_*` automations stay in HA under Settings →
Automations; disable or delete them there. If you disabled your own automations during the
migration, switch them back on there too (Migration shows which ones).

## Reporting

Settings → General → **Download bundle** (no tokens or keys) → `owner-reports/diagnostics/`.
Traces you disagree with: Activity → Runs → *Copy as test case* → `owner-reports/<milestone>/`.

## Simulator

`make sim` runs the real add-on inside a simulated home on your computer: no HA, Pi or Jetson needed.

| URL | What |
|---|---|
| `http://127.0.0.1:8100/` | the add-on GUI, behind a fake ingress like in HA |
| `http://127.0.0.1:8090/` | the **sim panel**: steer the house, the event flow, faults and the model backends |

The panel (`sim/control.py`, `sim/panel/`) has these tabs:

- **House**: size presets (`minimal` 30 entities, `realistic` ~330, `large` ~1300) built from
  Zigbee2MQTT devices (Aqara, IKEA, Tuya, Heiman), a router with its client trackers, and HA's own
  housekeeping entities. Junk is included: unrenamed `0x…` devices, `_2` duplicates left behind
  after re-pairing, orphaned `restored` entities, disabled diagnostics, dead batteries. The same
  seed always builds the same house. A rebuild sends `state_changed` and registry events the way
  an integration reload does.
- **Flow & events**: rate (pause … ×10), a 30-second storm, the owner's state-trigger automations
  on or off, and history: 7, 14 or 30 days of routines written into history and the logbook
  without waiting. Live changes are listed with their cause: *user*, *physical*, *automation* or
  *agent_house*.
- **Entities**: search, filter to unavailable, unknown or orphaned entities, set a state with a
  cause, remove an entity.
- **Scenarios**: arrive home, leave home, evening, night, window open while heating, smoke, water
  leak, Zigbee outage, router reboot, presence flapping.
- **Faults**: HA (WebSocket drop, restart, 500s, slow REST), the room-agent LLM (errors, latency;
  for the fake: loading, garbage, hang, broken tool-call JSON), Claude and OpenAI errors (429, 529,
  500), fake Jetson metrics (overheating, out of RAM).
- **Models & keys**: what serves each model port. The add-on always talks to the same local
  gateways; the panel switches them while the sim runs, without a restart:

  | Port | Fake by default | Can switch to |
  |---|---|---|
  | `:8080` room agents (llama-server API) | scripted fake | the real Jetson by IP · an OpenAI-compatible API (for example DeepInfra running Qwen3.5-4B, the Jetson's model) · OpenAI · Claude |
  | `:8081` orchestrator, Claude | fake | `api.anthropic.com` |
  | `:8082` orchestrator, OpenAI | fake | `api.openai.com` |
  | `:9100` Jetson metrics | fake | the real Jetson (*auto* follows the room backend) |

  With a cloud backend, *Pace* slows answers to the Jetson's measured speed (515 tok/s prompt, 16
  tok/s generation, D23). It also serves one request at a time (`-np 1`) and rejects prompts that
  would not fit 8192 tokens, the same error llama-server returns. Keys are stored in
  `.data/sim/secrets.json` (mode 600, gitignored). Only the gateways use them: the add-on keeps its
  `sk-ant-sim` placeholders, and the panel shows the last four characters of a key at most.
  **Test** buttons call free endpoints (model lists, `/health`). The add-on's daily budget still
  limits real orchestrator calls. Room-agent calls through a cloud backend are not counted against
  that budget, so watch the **Requests** tab.
- **Requests**: every model call through the gateways: backend, model, status, latency, tokens.
  Bodies and keys are not logged.

The panel accepts only `127.0.0.1`/`localhost` and requires an `X-Sim-Panel` header on writes.
Settings persist in `.data/sim/config.json`. `python -m sim.run --house large --backfill-days 30`
overrides them for one run; `--quiet-house` starts with the flow paused, `--speed 10` plays
scenario waits ten times faster. `--port-base 0` puts all eight ports on free ones (or `N` on
N..N+7) so several sims run side by side; give each its own `--data-dir`. `make test` never
touches a real backend.

### Polygon: headless runs with checks

`make polygon` (or `python -m sim.polygon run --scenario all`) boots the same stack without a
browser, on free ports and in its own data dir under `.agent-logs/polygon/<stamp>/`. For every
scenario it builds a fresh house and add-on, seeds the agents from `sim/polygon.yaml`, plays the
scenario (×10 by default), waits until the runs settle and checks:

| Check | Fails when |
|---|---|
| `shadow_no_service_calls`, `shadow_no_executed_actions` | in shadow, the add-on called an HA service or executed an action |
| `calls_match_actions` (`--live`) | HA saw more service calls than there are executed actions |
| `t3_never_actuated` | a T3 action was executed or approved |
| `owner_automations_untouched` | an automation without the `agent_house_` prefix was written, or one of the owner's changed |
| `known_entities_only` | an action names an entity that does not exist |
| `runs_without_errors`, `llm_requests_ok` | a run ended in error/timeout, a model call answered ≥ 400 |
| `expected_wakes`, `no_unexpected_wakes` | the gate did (not) wake the agents listed for that scenario in the spec |

`report.json` holds, per scenario, the full run traces, actions, feed, inbox, the service calls
and config writes HA received, and the gateway request log. Exit code 0 = all checks passed,
1 = a check failed, 2 = setup failed (bad spec, missing key, backend unreachable).
`python -m sim.polygon serve` boots one polygon and keeps it up, printing its URLs as JSON: a
testing agent can drive the add-on API through `urls.app` and the house through the panel API
(`urls.panel`, writes need `X-Sim-Panel: 1`). `--shared` plays all scenarios in one house, so
cooldowns and wake caps carry over.

Model backends are the fakes unless a flag says otherwise; the panel's saved backend choice is
ignored, so a forgotten `real` cannot cost money. Real models go through the same gateways:

```
export OPENAI_API_KEY=… ANTHROPIC_API_KEY=…      # or save them in the sim panel once
python -m sim.polygon run --scenario all --room-llm openai --orchestrator anthropic
python -m sim.polygon run --scenario arrive_home --room-llm jetson --jetson-host 192.168.178.40
python -m sim.polygon run --scenario all --room-llm compatible --room-model Qwen/Qwen3.5-4B \
    --compatible-url https://api.deepinfra.com/v1/openai   # key: AH_SIM_COMPATIBLE_API_KEY
```

Each real backend is checked with a free call before the house boots. Cloud room models are paced
to the Jetson's speed unless `--no-pace`; a full run is then minutes, not seconds. Pacing on is
the honest setting for "would this work on the Jetson". The fake room model answers by rule
(motion + a light in the prompt → switch it on), so with the fakes the polygon tests the code
(gate, policy, shadow, traces), and with a real model it also tests the behaviour.
