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
