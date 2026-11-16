# Agent House

Learns how your house is used and writes the automations for you. A learner on your Jetson keeps
the house's history and learns habits, routines and the corrections you make; a cloud model (Claude
or OpenAI) drafts automations from that for you to approve and looks after the house between the
daily reviews. Reflexes stay in Home Assistant: an approved automation keeps running when this
add-on, the Jetson or the internet is down.

**Version 0.5.0 (prototype).** Room agents and the local model of earlier versions are gone (D40).
The orchestrator drafts automations; you review, simulate, deploy, version and roll them back on
**Automations → Drafts** and **Automations → Deployed**. A daily review (04:30 by default) scans the
history for habits, undone and fighting automations and drafts one proposal per finding; the house
manager (off by default) checks the house in between. *Migration* imports your own automations and
Node-RED flows, *Chat* talks to the orchestrator. Day-to-day operation and what to do when something
goes wrong: `docs/RUNBOOK.md` in the repository.

## Safety model

- **Shadow Mode is the default.** The learner and the house manager log what they would do.
  Nothing is actuated until you switch the global mode to Live *and* set `learner_mode` or
  `orchestrator_mode` to live in Settings.
- **Kill switch** in the top bar (or the HA event `agent_house_kill_switch`): while engaged, no
  service call, no deploy, no draft and no model call leaves the add-on.
- **One path to Home Assistant**: every action of the learner (agent `learner`) and of the house
  manager (agent `orchestrator`) goes through the same policy and action log. The learner acts only
  on devices its fresh model names, never on helpers such as the house mode.
- **Tiers** decide what may be touched (deny by default):

  | Tier | Meaning | Default domains |
  |---|---|---|
  | T0 | free | light, media_player, fan |
  | T1 | act and notify you | humidifier, vacuum, the house mode helper |
  | T2 | act only after your approval | climate, cover, switch, scene, input_boolean, input_select, water_heater, input_number |
  | T3 | never | lock, alarm_control_panel, valve, siren, camera, garage covers, anything not listed |

- Automations are **drafts** until you approve them. Each draft is checked (YAML, schema, safety
  policy, Home Assistant's own validation); T3 entities, events, scripts and area-wide targets are
  refused. Deployed ones get an `agent_house_` id and a version history. Your own automations are
  never edited or deleted.
- **Cloud calls** (Claude or OpenAI, only with an API key) carry redacted text and stop at `daily_budget_eur`.

## Configuration

| Option | Meaning |
|---|---|
| `jetson_learner_url` | The learner on the Jetson (`jetson/learner`), e.g. `http://192.168.1.50:9200`. Empty: no learner, the add-on's own history scan finds the habits. |
| `jetson_token` | The learner's token (`/etc/agent-house/token` on the Jetson). |
| `orchestrator_provider` | `auto` (Claude, then OpenAI — only those with a key, only while the budget lasts), `anthropic`, `openai`. `local` from before 0.5.0 works like `auto`. |
| `anthropic_api_key` | Only for the orchestrator. Never shown in the GUI or the diagnostics bundle. |
| `anthropic_model` | Claude model id, default `claude-haiku-5-5` (cheapest; `claude-sonnet-5-5` or `claude-opus-5-5` cost more and draw down the budget faster). |
| `openai_api_key` | Optional second cloud for the orchestrator. Never shown in the GUI or the diagnostics bundle. |
| `openai_model` | OpenAI model id, default `gpt-6.1-sol` (mid tier, priced like Claude Sonnet); `gpt-6-luna` is the cheap one. |
| `daily_budget_eur` | Hard daily stop for cloud spending. |
| `redact_cloud` | Replace people's and zones' names, person/tracker ids, addresses, coordinates, IPs, URLs, phone numbers and secrets in cloud prompts. |
| `log_level` | `debug`, `info`, `warning`, `error`. |

Node-RED exports for the migration assistant (M5) go into `/share/agent_house/nodered/`; the
add-on mounts `/share` read-only.

## Reporting a problem

**Settings → General → Download bundle** (Diagnostics). The zip contains status, redacted settings,
a registry summary, the recent feed, the learner's state and the log — no tokens or API keys. Commit it to
`owner-reports/diagnostics/` in the repository.

## Going back to plain Home Assistant

Stop or uninstall the add-on. Automations it deployed stay in Home Assistant under
*Settings → Automations* with ids starting `agent_house_`; disable or delete them there like
any other automation.
