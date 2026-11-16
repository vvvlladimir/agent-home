# Runbook

How to run Agent House day to day and what to do when something goes wrong. Install steps and
options: [`agent_house/DOCS.md`](../agent_house/DOCS.md). Hardware setup and the owner's test steps:
[`OWNER_CHECKLIST.md`](OWNER_CHECKLIST.md). Why things are the way they are:
[`DECISIONS.md`](DECISIONS.md).

## The pieces

| Piece | Runs on | If it is down |
|---|---|---|
| Home Assistant + deployed `agent_house_*` automations | Pi 5 (HA OS) | nothing works; HA is the source of truth |
| Agent House add-on (GUI, policy, orchestrator, house manager, learner link) | Pi 5, Supervisor add-on | deployed automations keep running in HA; nothing of ours acts |
| The learner | Jetson Orin Nano (`agent-house-learner.service`) | the add-on buffers what happens and backfills from the logbook later; habits come from its own 14-day scan (`predict.py`); no live suggestions |
| Claude / OpenAI API | cloud | no drafts, chat, explanations or house manager runs; the daily review is the digest without a summary; spend stops at `daily_budget_eur` |

State lives in `/data` of the add-on: `agent_house.db` (SQLite: runs, traces, actions, proposals,
automation versions, findings, spend, the learner's last house model), `settings.yaml` (GUI
settings) and `policy.yaml`. A Home Assistant backup that includes the add-on saves all of it. The
learner's journal lives on the Jetson (`--data`, on the NVMe).

## Safety levers, from gentle to hard

1. **An actor to shadow** — Settings → *Learner mode* / *Orchestrator mode* → shadow. It keeps
   logging what it would do.
2. **Global mode to Shadow** — top bar. The learner and the house manager are in shadow, whatever
   their own mode says.
3. **Kill switch** — top bar, or from HA: fire the event `agent_house_kill_switch` (or switch the
   `input_boolean` named in `kill_switch_entity` on). No service call, no deploy, no draft, no
   model call, no review leaves the add-on. Already deployed automations keep running in HA (S7).
4. **Disable one of our automations** — Automations → Deployed → untick *On* (or roll back to an
   earlier version).
5. **Stop the add-on** — Settings → Add-ons → Agent House → Stop. HA runs on, with only its own
   automations and the `agent_house_*` ones.

Releasing the kill switch does not switch anything back on by itself: the learner and the manager
act again only on their next suggestion or run, and only where policy and mode allow.

## Going from Shadow to Live (per actor)

Do this one actor at a time, after a few days of shadow actions that look right.

1. Activity → actions filtered by `learner` (or `orchestrator`): every `shadow` action should be
   one you would have wanted. The learner acts only on devices its fresh model names, never on
   helpers such as the house mode; the house manager on the switchable devices of the house.
2. Check the tiers: T0 acts silently, T1 acts and notifies, T2 waits for your approval on the
   Overview, T3 never acts.
3. Set the global mode to **Live**, then *Learner mode* or *Orchestrator mode* to **live**.
4. Watch the live feed for an hour. Anything odd: back to shadow (lever 1).

The orchestrator switches `input_select.agent_house_mode` when `auto_house_mode` is on and the
orchestrator mode is live; it never leaves `guests` or `work`, which are yours. With the house
manager on (`manager_enabled`), it may also switch devices when its sweep finds something (lights
on with nobody home …) — the model is called only then, at most `manager_max_wakes_per_hour`.

## Daily routine (two minutes)

- **Inbox** (dashboard): T2 approvals waiting, the house manager's reports, unusual-state notes and
  the orchestrator's *Daily review* (default 04:30, `daily_review_at`).
- **Automations → Drafts**: drafts from the review, the house manager and *Found in the history*.
  Each has four checks (yaml, schema, policy, `ha_validate`); a `blocked` one cannot be approved.
  Simulate against history before approving. A proposal that `supersedes` another automation of
  ours switches that one off when it is deployed (you can switch it back on).
- **Council**: house mode, the learner (journal, training, what it found), who owns what.

## Incidents

| Symptom | Look at | Do |
|---|---|---|
| A light or device switches on its own | Activity → Live feed (actor and run id) → Runs → trace; Automations → Deployed | Shadow the actor (learner / orchestrator mode), or disable the automation; report the trace |
| An automation of ours keeps getting undone | Automations → Drafts → *Found in the history* (`undo` findings); the review suggests a fix, above 60 % a rollback | Approve the fix or roll back under Automations → Deployed |
| Two automations switch one light back and forth | *Found in the history* → `conflict` finding | Approve the conflict proposal: it replaces one of ours and supersedes the other; switch your own one off from Migration if it is one of yours |
| The house manager never runs | Settings → *House manager* on? `api/manager` (prefilter, wakes in the last hour), feed lines `manager:` | Nothing found means no model call (by design); check the hourly cap and the budget |
| The learner shows *not connected* | `api/learner` → `link.error`; Jetson: `systemctl status agent-house-learner`, `journalctl -u agent-house-learner -n 100` | Check `jetson_learner_url` and `jetson_token`; `sudo systemctl restart agent-house-learner`; the add-on backfills what it missed |
| The learner's model is stale (> 48 h) | `api/learner` → `jetson.training` | Its habits and live suggestions are ignored until it trains again; *Train now* on the Council page |
| Cloud drafts stopped | Connection status in the header (hover it), Settings → General → Add-on options | Budget reached: wait for the next day or raise `daily_budget_eur`; 401: check the key |
| Kill switch engaged and you did not do it | Feed line naming the source (GUI / HA event / helper) | Find who fired it (HA logbook) before releasing |
| GUI does not load | Add-on log; open it only through the HA sidebar (direct ports are refused, S5) | Restart the add-on; send a diagnostics bundle |

## Upgrades and rollback of the add-on

- Read `agent_house/CHANGELOG.md`, then Settings → Add-ons → Agent House → Update. The database
  migrates itself on start; there is no downgrade path for it, so take an HA backup first.
- To return to an earlier add-on version, restore that backup (partial backup of the add-on).
- Settings → **Config export** saves the policy and the GUI settings as YAML (no secrets). An export
  from before 0.5.0 still imports; its room agents are ignored and named in the answer.
- 0.4 → 0.5 (D40): the room agents' files in `/data/agents`, the Jetson model options and the model
  settings are ignored; `orchestrator_provider: local` works like `auto`. On the Jetson run
  `jetson/learner/setup_learner.sh --stop-llm` once to stop llama-server.

## Leaving

Stop or uninstall the add-on. The `agent_house_*` automations stay in HA under Settings →
Automations; disable or delete them there. If you disabled your own automations during the
migration, switch them back on there too (Migration shows which ones).

## Reporting

Settings → General → **Download bundle** (no tokens or keys) → `owner-reports/diagnostics/`.
Traces you disagree with: Activity → Runs → the run's id and the trace → `owner-reports/<milestone>/`.
`api/dev` (Developer tools) shows the add-on's internals in one snapshot, secrets masked.

## Simulator

`make sim` runs the real add-on inside a simulated home on your computer: no HA, Pi or Jetson needed.
The learner is the real `jetson/learner`, running in the same process on the add-on's house time,
wired through the add-on's normal options (`jetson_learner_url`, `jetson_token`). It trains once on
start (when the journal has events) and every night at 03:30; its journal is in
`<data-dir>/learner`.

| URL | What |
|---|---|
| `http://127.0.0.1:8100/` | the add-on GUI, behind a fake ingress like in HA |
| `http://127.0.0.1:8090/` | the **sim panel**: steer the house, the event flow, faults, the learner and the cloud backends |

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
- **Faults**: HA (WebSocket drop, restart, 500s, slow REST), the learner gateway (errors, latency),
  Claude and OpenAI errors (429, 529, 500).
- **Models & keys**: the learner (its journal, training, live targets, what its model holds;
  **Train now**) and which learner the add-on reaches; what serves each cloud port. The add-on
  always talks to the same local gateways; the panel switches them while the sim runs, without a
  restart:

  | Port | Default | Can switch to |
  |---|---|---|
  | `:8080` the learner | the learner in this process | the real Jetson by IP (`:9200`, its token) · off (down: the add-on falls back to its own scan) |
  | `:8081` orchestrator, Claude | fake | `api.anthropic.com` · `claude -p` |
  | `:8082` orchestrator, OpenAI | fake | `api.openai.com` |

  Keys are stored in `.data/sim/secrets.json` (mode 600, gitignored). Only the gateways use them:
  the add-on keeps its `sk-ant-sim` / `jetson-sim` placeholders, and the panel shows the last four
  characters of a key at most. **Test** buttons call free endpoints (model lists, the learner's
  `/status`). The add-on's daily budget still limits real orchestrator calls.
- **Requests**: every call through the gateways: backend, model, status, latency, tokens.
  Bodies and keys are not logged.

The panel accepts only `127.0.0.1`/`localhost` and requires an `X-Sim-Panel` header on writes.
Settings persist in `.data/sim/config.json` (a config from before D40 still loads; its room-agent
part is dropped). `python -m sim.run --house large --backfill-days 30` overrides them for one run;
`--quiet-house` starts with the flow paused, `--speed 10` plays scenario waits ten times faster.
`--port-base 0` puts all seven ports on free ones (or `N` on N..N+6) so several sims run side by
side; give each its own `--data-dir`. `make test` never touches a real backend.

### Polygon: headless runs with checks

AI testers that use the add-on like an owner, and the accuracy metrics: `docs/POLYGON.md`.

`make polygon` (or `python -m sim.polygon run --scenario all`) boots the same stack without a
browser, on free ports and in its own data dir under `.agent-logs/polygon/<stamp>/`, with the
learner in-process. For every scenario it builds a fresh house and add-on, applies the settings of
`sim/polygon.yaml`, plays the scenario (×10 by default), wakes the house manager once (with the
spec's `manager_script` queued on the fake Claude), waits until the runs settle and the learner has
the scenario's events, and checks:

| Check | Fails when |
|---|---|
| `shadow_no_service_calls`, `shadow_no_executed_actions` | in shadow, the add-on called an HA service or executed an action |
| `calls_match_actions` (`--live`) | HA saw more service calls than there are executed actions |
| `t3_never_actuated` | a T3 action was executed or approved |
| `owner_automations_untouched` | an automation without the `agent_house_` prefix was written, or one of the owner's changed |
| `known_entities_only` | an action names an entity that does not exist |
| `known_actors_only` | an action is by anyone but `orchestrator` or `learner` |
| `runs_without_errors`, `llm_requests_ok` | a run ended in error/timeout, a cloud model call answered ≥ 400 |
| `learner_link_ok`, `learner_events` | the add-on did not reach the learner or lost events; a change the spec names is not in its journal |
| `expected_actions` (`--live`) | the manager did not switch what the spec's script asked for |

`report.json` holds, per scenario, the full run traces, actions, feed, inbox, the service calls
and config writes HA received, the gateway request log and the learner link. Exit code 0 = all
checks passed, 1 = a check failed, 2 = setup failed (bad spec, missing key, backend unreachable).
`python -m sim.polygon serve` boots one polygon and keeps it up, printing its URLs as JSON: a
testing agent can drive the add-on API through `urls.app` and the house through the panel API
(`urls.panel`, writes need `X-Sim-Panel: 1`). `--shared` plays all scenarios in one house.
`python -m sim.polygon days --days 30` lives a household in house time; the learner trains every
house night at 03:30 (`sim/days.py`). Stories: `python -m sim.stories run` (docs/POLYGON.md §4c).

The clouds are the fakes and the learner is the in-process one unless a flag says otherwise; the
panel's saved backend choice is ignored, so a forgotten `real` cannot cost money:

```
export OPENAI_API_KEY=… ANTHROPIC_API_KEY=…      # or save them in the sim panel once
python -m sim.polygon run --scenario all --orchestrator anthropic
python -m sim.polygon run --scenario all --orchestrator claude            # `claude -p`, no key (D30)
python -m sim.polygon run --scenario arrive_home --learner jetson --jetson-host 192.168.178.40
python -m sim.polygon run --scenario all --learner off                   # the learner down
```

Each real backend is checked with a free call before the house boots. With the fakes the polygon
tests the code (policy, shadow, actuator, the learner link, traces); with a real model it also
tests the manager's behaviour.
