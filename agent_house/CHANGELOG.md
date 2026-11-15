# Changelog

## 0.3.0

- **Habits are predictions now (D35).** Instead of "you switched it at about 19:57 on 10 days, so
  switch it at 19:57", the add-on learns *in which situation* you switch a device (motion, another
  device, people, sun, helpers, time of day as one of many signals), checks every rule on days it had
  not seen, and only then proposes: **act** when it is right often enough for what a mistake costs,
  **ask first** (a phone notification with a button) when it is fairly sure, else nothing. A TV you
  switch on when you feel like it is never proposed; nothing is blocked by device type.
- What a mistake costs is learnt from how fast you undo our automations; an ask you keep accepting
  becomes an automation that acts and replaces the ask. At most about 3 asks a day.
- Habit proposals are built without a model, from the counted rule; the rationale says how often it
  was right. History scanned by default: 14 days.
- A question you ignore and then do yourself disappears from the phone and counts as a right guess,
  so it comes back surer next time.
- Agents are woken less by events you never react to (motion that never leads to anything you do):
  at most every 30 minutes. Alarms are never slowed.
- **Unusual states** in the inbox: a device in a state it is almost never in at this time of day
  (the lamp on at 03:00), after 30 minutes. Nothing is switched.
- Your own house-mode switches (away when you leave) can become proposals too.
- **Pick the model** for each request: a new automation, a change, an explanation can each run on
  the Jetson or a cloud (Claude, OpenAI), whatever the default in Settings is.
- **Change it in words**: on any automation, write what should change; the chosen model edits the
  draft (or drafts a changed copy of a deployed or rejected one). Nothing is deployed by it.
- **In plain words**: a model explains what an automation does in a few sentences, in your language.
- **Flow editor**: every automation as a chart, top to bottom — when / only if / do, with forks for
  if-else and choose. Click a card to edit it, drag it to move it, "+" to add a trigger, condition or
  step anywhere, switch a part off, undo, zoom. Deployed automations get an "Edit" button.

## 0.2.0

- **GUI rewritten** in Vue 3 + TypeScript: six tabs, an overview of decisions, rooms and today,
  plus accuracy and house manager cards.
- **House manager**: watches the board, stops re-sending unchanged findings and task-escalation
  loops, stops on budget with a notice; failed runs name their event, reports cannot claim what
  did not happen, no tasks in shadow.
- **Habits**: habit dossiers and a first look before a proposal; recognizes who uses a program
  and covered states; guest evenings and short bursts are no longer habits.
- New agents get **suggested wake rules** from the room's devices.
- New option **T3 exceptions** (`t3_whitelist`): exact entity ids from a T3 domain (locks,
  alarms) that agents may act on as T2, every action waiting for your approval.
- Fixed: referenced entities are validated; unwatched alarms are handled; debounces settle on
  the last state; shadow mode records the way back; QA-001…QA-010 safety and UX findings.

## 0.1.0 — first release (prototype)

- **Council** page: the house mode helper `input_select.agent_house_mode` (create it with one
  click), the shared board where rooms post short facts, tasks, reports and escalations, which
  agent owns which entity, what wakes the orchestrator, and the daily review.
- **One owner per entity**: a room agent may act on and automate only its own room; everything
  without an area belongs to the orchestrator, which itself only switches the house mode helper
  (in shadow unless `orchestrator_mode: live`). `ask_agent` and escalations go through the
  orchestrator to the owner, at most 6 per hour, never from a task.
- **Daily review** (04:30, `daily_review_at`): scans the logbook for habits, automations you undo
  and automations that fight, summarises the day on the local model and drafts at most three
  proposals, one per entity. A conflict between two of ours becomes one proposal that
  **supersedes** the other: approving it switches that one off (you can switch it back on).
- Optional **automatic house mode** (home / away / night from people and the night window);
  guests and work stay yours.
- Room agents default to `qwen35-4b-balanced` after the benchmark on the Jetson.
- Fixed: right after a model switch the status could say `ok` without a model name.
- `docs/RUNBOOK.md`: daily routine, safety levers, going live room by room, incidents.

## 0.1.0-dev.10 — M5 migration

- **Migration** page: imports your own automations, scripts and Node-RED exports
  (`/share/agent_house/nodered/*.json`) read-only, asks the orchestrator for a replacement (a normal
  proposal), compares both against the last 7 days of history, and can switch your original off
  and on again. Nothing of yours is edited or deleted.
- **Chat** page: talk to the orchestrator (it looks things up and drafts automations, never
  switches anything) or to a room agent (a normal run: same tiers, same shadow mode).
- Settings: **Config export / import** (agents, policy, GUI settings as YAML, no secrets).
  Imported agents always start in shadow mode.

## 0.1.0-dev.9 — the orchestrator finds your sensors

- Fixed: drafts were declined with "there is no sensor for that" although it exists. The model saw
  only 80 entities, and every sensor was ranked behind all devices, so radar zones never made the
  list. Now half of the first look is reserved for room sensors (motion, occupancy, …), and the
  model has a `find_entities` search; it must search before it may decline.

## 0.1.0-dev.8 — OpenAI as a second cloud

- New options `openai_api_key` and `openai_model` (default `gpt-6.1-sol`, priced like Claude
  Sonnet). `orchestrator_provider: openai` uses only OpenAI; `auto` tries the local model, then
  Claude, then OpenAI — only the clouds you gave a key for. Same redaction, same daily budget.
- Jetson metrics: a thermal sensor that cannot be read no longer breaks `/metrics.json`
  (update the repository on the Jetson and restart `agent-house-metrics`).

## 0.1.0-dev.7 — M4 proposals and deployed automations

- **Proposals** page: describe an automation in words (the orchestrator drafts it) or paste YAML.
  Every draft is checked (YAML, schema, safety policy, Home Assistant's own validation), shows a
  diff against what is deployed and can be simulated against the last days of history. Nothing
  reaches Home Assistant until you press *Approve and deploy*.
- **Deployed automations** page: everything Agent House deployed (`agent_house_*` ids), with
  versions, rollback, enable / disable and remove. Your own automations are never changed.
- The orchestrator uses the local model first and Claude (`claude-haiku-5-5` by default) when the
  local model is down; cloud prompts are redacted and stop at the daily budget.
- Room agents can suggest an automation (`propose_automation`); it lands on the Proposals page.
- The kill switch also stops drafting, deploying, rollbacks and enabling automations.
- From the M3 run: device settings (`number.*_off_delay`, countdowns) no longer push room sensors
  out of the prompt; people walking in is no longer reported as a warning; the Jetson metrics
  endpoint answers errors instead of dropping the connection.

## 0.1.0-dev.6 — agents see the right entities

- The prompt lists what matters first: entities in the agent's wake rules, then the ones it has
  read or used before, then working devices; unavailable devices and indicator LEDs go last. A
  quarter of the list is kept for room sensors (motion, temperature, doors, windows).
- Agents may re-read states as often as they need; repeating an action is refused once, then the
  run stops.
- No more agent ping-pong: one `schedule_wake` per run, and no `schedule_wake` / `ask_agent` on a
  wake caused by another agent or a schedule.
- The default prompt explains "check your room": read, answer in one sentence if all is fine.

## 0.1.0-dev.5 — pinned entities

- Agents editor → *Ticked entities*: "only these" (as before) or "first in the prompt + search the
  whole room": the ticked entities always lead the prompt, the rest of the room stays findable.

## 0.1.0-dev.4 — smarter entity selection

- Room agents skip device noise (firmware updates, trackers, battery, signal and voltage sensors);
  buttons stay.
- The prompt lists the most useful entities first (lights and fans, then other devices, then
  motion/temperature/door sensors). The rest of the room stays reachable: the agent finds it with
  a search in `list_entities`.

## 0.1.0-dev.3 — M3 room agents in Shadow Mode

- Room agents: create and edit them in the GUI (room, entities and tiers, wake rules, schedules,
  limits, prompt preview with a token estimate). Every agent starts in Shadow Mode.
- Runs & traces: every wake with its prompt, model replies, tool calls and policy verdicts;
  replay in shadow mode and copy a run as a test case.
- Policy engine and a single actuation path: T2 actions wait for your approval on the Dashboard,
  T3 is never actuated, every attempt is in the action log.
- Kill switch from HA: the `agent_house_kill_switch` event or an optional `input_boolean` helper.
- Scenes and `input_boolean` helpers are now T2 (approval) by default.
- Live feed falls back to polling when the event stream cannot connect.

## 0.1.0-dev.2 — M2 inference stack

- LLM manager for the Jetson's llama-server: priority queue, coalescing, stale drop, retries,
  warming state while a model loads, rolling speed metrics; tool-call JSON repair.
- Models & Benchmarks page: profiles, switching the resident model, a benchmark runner with history
  (tok/s, time to first token, wake latency, tool-call accuracy, RAM, temperature).
- Settings: local model request limits and the presence penalty are editable in the GUI.

## 0.1.0-dev.1 — M1 skeleton

- Add-on installs, opens through Ingress and connects to Home Assistant (read-only).
- Dashboard: global mode (Shadow by default), kill switch, HA / Jetson / orchestrator status, rooms.
- Live feed of state changes, entity browser with permission tiers, log viewer, diagnostics bundle.
- No agent runs and nothing is actuated in this build.
