# M3 owner run (2026-11-13, 0.1.0-dev.6, qwen35-2b-fast)

Evidence: `../screenshots/m3-runs.png`, `../diagnostics/agent-house-diagnostics-20261113-104148.zip`,
three copied runs (`*-case.yaml`). About one hour of Shadow, not a full day.

| What | Result |
|---|---|
| Runs | 12: 9 `ok`, 3 `escalated`, all `shadow`; event wakes from both radar sensors arrived |
| Actions | none: no agent asked to actuate anything (Action log empty) |
| Latency | 1.7–22 s per run, 1–5 steps, 2–12 k prompt tokens |
| Loops | 09:37 ask_agent ping-pong and repeated list_entities: fixed in 0.1.0-dev.6 (S11, S12) |
| Jetson metrics | unreachable: the endpoint dropped the connection (an exception in the handler, now answered as HTTP 500 with the error) |

Findings fixed afterwards:

- Living room prompt: six T3 device settings (`number.*_off_delay`, `number.*_countdown`) sat ahead of
  the room sensors. Settings domains now rank after the sensors (S13,
  `tests/regressions/livingroom_owner_run_2.yaml`).
- r_1a1d43953a55cf263 sent a `warning` because someone entered the room, and summaries claimed
  states the model had not read. The default prompt now says occupancy changes alone are not worth a
  report and to describe only states that were read.
- Copy-as-test-case labelled a run with an `info` report as `escalate`; only `warning`/`escalate`
  reports count now.
- The case files arrived as `.log`; renamed to `<room>-<wake>-case.yaml`.
