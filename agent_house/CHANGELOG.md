# Changelog

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
