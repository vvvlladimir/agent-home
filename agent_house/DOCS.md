# Agent House

LLM agents for your smart home: one small agent per room watches what happens, a bigger
orchestrator sees the whole house and writes automations for you to approve. Reflexes stay in
Home Assistant: an approved automation keeps running when this add-on, the Jetson or the
internet is down.

**This build is M4 (development).** Room agents run in Shadow Mode, the orchestrator drafts
automations, and you review, deploy, version and roll them back on the *Proposals* and
*Deployed automations* pages. Migration from your own automations and the chat arrive in M5.

## Safety model

- **Shadow Mode is the default.** Agents log what they would do. Nothing is actuated until you
  switch the global mode to Live *and* set an individual agent to live.
- **Kill switch** in the top bar: while engaged, no service call and no deploy leaves the add-on.
- **Tiers** decide what an agent may touch (deny by default):

  | Tier | Meaning | Default domains |
  |---|---|---|
  | T0 | free | light, media_player, fan |
  | T1 | act and notify you | scene, input_boolean, humidifier, vacuum |
  | T2 | act only after your approval | climate, cover, switch, water_heater, input_number |
  | T3 | never | lock, alarm_control_panel, valve, siren, camera, garage covers, anything not listed |

- Automations written by agents are **drafts** until you approve them. Each draft is checked
  (YAML, schema, safety policy, Home Assistant's own validation); T3 entities, events, scripts and
  area-wide targets are refused. Deployed ones get an `agent_house_` id and a version history.
  Your own automations are never edited or deleted.
- **Cloud calls** (Claude or OpenAI, only with an API key) carry redacted text and stop at `daily_budget_eur`.

## Configuration

| Option | Meaning |
|---|---|
| `jetson_llm_url` | `llama-server` on the Jetson, e.g. `http://192.168.1.50:8080`. Empty until the Jetson is set up. |
| `jetson_metrics_url` | Metrics endpoint from `jetson/metrics`, e.g. `http://192.168.1.50:9100`. Optional. |
| `jetson_token` | Shared secret for switching model profiles. Optional. |
| `orchestrator_provider` | `auto` (the local model first; when it is down Claude, then OpenAI — only those with a key, only while the budget lasts), `anthropic`, `openai`, `local`. |
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

Settings & Logs → **Download diagnostics bundle**. The zip contains status, redacted settings,
a registry summary, the recent feed and the log — no tokens or API keys. Commit it to
`owner-reports/diagnostics/` in the repository.

## Going back to plain Home Assistant

Stop or uninstall the add-on. Automations it deployed stay in Home Assistant under
*Settings → Automations* with ids starting `agent_house_`; disable or delete them there like
any other automation.
