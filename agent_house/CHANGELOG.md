# Changelog

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
