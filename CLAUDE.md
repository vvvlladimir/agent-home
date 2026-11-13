# Agent House — working rules

LLM-agent home control as a Home Assistant add-on. The add-on runs on HA OS (Pi 5); local
inference runs on a Jetson Orin Nano 8 GB over HTTP. Plan: `docs/PLAN.md`. Facts:
`docs/RESEARCH.md`. Choices: `docs/DECISIONS.md`. Hardware steps: `docs/OWNER_CHECKLIST.md`.

## Non-negotiable
- Shadow Mode is the default; nothing actuates until the owner enables it per agent.
- Tiers T0–T3, deny by default. T3 is never actuated. Global kill switch.
- Agents only reach HA through the tool layer → policy engine → trace.
- Automations are drafts until approved; deployed ids start with `agent_house_`.
  Never edit or delete the owner's own automations.
- A cheap deterministic prefilter decides whether any LLM is called.
- Cloud calls carry redacted summaries only.

## Rules for every agent
1. Contracts first: code against `contracts/` (OpenAPI + JSON Schemas), not other agents' source.
2. Stay in your lane (see `.claude/agents/`); cross-boundary changes go through the lead.
3. Return summaries: ≤150 words + file paths. Long logs go to `.agent-logs/`.
4. The plan lives in `docs/PLAN.md` with checkboxes; update it, don't re-explain it.
5. Run the narrowest check that proves the change:
   `make lint` · `make gui` · `make test` · `make sim` · `make addon-lint` · `make shellcheck` · `make ci`.
6. No speculative abstractions. Prototype quality, except the safety layer (policy,
   shadow mode, rollback), which must be correct and tested.
7. Use `scout` (haiku) for lookups and test runs; opus only for core logic and safety,
   or after two failed sonnet attempts.
8. Small conventional commits; one milestone = one tag.
9. Cloud-only honesty: no HA, Pi or Jetson here. Hardware-dependent claims are tagged
   `[UNVERIFIED-ON-HW]` and listed in `docs/OWNER_CHECKLIST.md`. Done = simulator/CI green
   plus owner steps written down.
10. Tests are hermetic: no network; simulators run in-process or via `make sim`.

## Layout
`agent_house/` add-on (`app/core`, `app/ha`, `app/api`, `app/web`, `app/store`) ·
`jetson/` (runs on the Jetson) · `contracts/` · `sim/` · `tests/` · `docs/` · `owner-reports/`.
Python ≥3.12 syntax (add-on image ships 3.14); imports are `app.*` and `sim.*` (pytest sets the path).
GUI: Vue 3 + TypeScript in `agent_house/frontend/`; `make gui` builds it into `app/web/static/` (committed,
so the image needs no Node). Relative URLs only, no CDN, `vue` is the only runtime dependency.
