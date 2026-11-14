---
name: polygon-claude
description: Run the AI-tester polygon from this session - polygons in the background, testers and verifiers as subagents, this session as lead and coordinator - and report the confirmed bugs. No API keys or Jetson. Use when the owner asks to run the polygon, the AI testers, or "find the bugs".
---

# Polygon with subagent testers (D30)

You are the lead. The polygons run here as background processes; the testers and verifiers are
your subagents (`qa-tester` haiku, `qa-attacker` sonnet, `qa-verifier` sonnet); you judge
duplicates yourself. Background: `docs/POLYGON.md` §4a. Use `.venv/bin/python` as `PY`.

## 1. Prepare
- `make install` if `.venv` is missing.
- Roles: the owner's list, else `newcomer migrator fussy_tenant`. `all` = seven roles; warn that
  seven testers plus the add-on's models can use up a Pro plan's usage window.
- Deterministic checks first: `make polygon-claude` (falls back: `make polygon`). A failing
  `shadow_*`, `t3_*` or `owner_*` check is critical: report it with `report.json` and stop.

## 2. Testers
- `PY -m sim.qa up --roles <roles>` → JSON with `out`, `models` and per role a `brief` path.
  `models: fake` means `claude -p` does not work here and the add-on's models are the fakes:
  say so in the report (model behaviour untested, code paths still are).
- Spawn one subagent per role **in one message, in the background**: `qa-attacker` for
  `intruder` and `chaos`, `qa-evaluator` for `hands_off_owner`, `qa-tester` for the rest. Prompt: `Your brief: <brief path>`.
- Wait for their notifications; do not poll. A tester that fails leaves no `findings.json`;
  note it and go on.

## 3. Coordinate (you)
- `PY -m sim.qa pairs <out>` → candidate pairs. For each, decide: same faulty behaviour under
  materially the same conditions? Unsure = different.
- `PY -m sim.qa coordinate <out> --same '<[[i, j], ...]>'` → `backlog.md`; note `rejected`.
- `PY -m sim.qa down <out>` (the testers' polygons).

## 4. Verify
- `PY -m sim.qa verify-up <out>` → a fresh polygon and `prompt` per candidate. A finding with a `snapshot` gets a polygon
  restored from it (D33); the prompt says so. More than 8
  candidates: verify `critical` and `major` first, the rest only if the owner wants.
- Spawn one `qa-verifier` per candidate in the background, prompt `Your task: <prompt path>`.
- Then `PY -m sim.qa verify-collect <out>` and `PY -m sim.qa down <out>`.

## 5. Report
- Copy `<out>/backlog.md` to `owner-reports/qa/<YYYY-MM-DD>-claude.md`; commit only if asked.
- If `hands_off_owner` ran, append its `<out>/hands_off_owner/assessment.md` to that file under
  `## Usefulness (hands_off_owner)` and give its one-sentence verdict in the answer.
- Answer the owner in at most 150 words: the `confirmed` items, most severe first (id, one line,
  endpoint), then counts of `not_reproduced`, unverified candidates and rejected findings, the
  `models` value, and the backlog path.

Do not fix bugs here; fixes go through the owning agent. Always run `down` before you finish,
even after an error.
