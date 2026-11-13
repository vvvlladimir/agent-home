# Owner checklist

Everything here needs your hardware. Nothing below has been verified by the cloud session; each
step says what to commit so the next session can read the result.

Where results go: `owner-reports/` (see its README). After committing, run `make analyze-report`
or just start the next session.

## Questions — answered 2026-11-13 (`owner-reports/answers.md`)

| # | Question | Answer |
|---|---|---|
| Q1 | Supervisor and HA Core versions? | Core 2026.11.1, Supervisor 2026.11.0 |
| Q2 | JetPack version on the Jetson? | L4T R36.5.0 → JetPack 6.x; scripts take the 6.x branch |
| Q3 | Which rooms first? | Bedroom, Livingroom (entities from the area registry) |
| Q4 | Perimeter entity not named like its domain? | None |
| Q5 | Node-RED flows as JSON? | Yes: `owner-reports/nodered/*.json` |
| Q6 | Daily Claude budget, and which model? | `claude-haiku-5-5`; local Qwen becomes the default once it works; budget €1/day (default kept) |
| Q7 | Jetson address? | `http://192.168.1.215` (port set in M2) |

## M1 — install the skeleton

Before starting: this build is read-only. It cannot switch anything in your home.

1. Home Assistant → Settings → Add-ons → Add-on Store → ⋮ → Repositories → add
   `https://github.com/vvvlladimir/agent-home` (while the work is on a branch, append
   `#claude/stoic-cray-1j91qm`).
2. Install **Agent House**. The Supervisor builds the image on the Pi; expect a few minutes.
3. Start it and open **Agent House** in the sidebar.
4. Check and note:
   - [x] the page renders with styling (if it is unstyled, a URL is not relative: tell me)
   - [x] the "HA" pill is green and shows your Core version
   - [x] Dashboard → Rooms lists your areas with plausible entity counts
   - [x] Agents page: locks, alarm panel, garage door show **T3**; lights show **T0**
   - [x] Live feed shows state changes within a second or two of them happening (if they arrive in
         bursts or not at all, SSE is being buffered by ingress: tell me)
   - [x] the kill switch toggles and survives an add-on restart
5. Commit to `owner-reports/`:
   - `screenshots/m1-dashboard.png`
   - `diagnostics/<date>.zip` from Settings & Logs → Download diagnostics bundle
   - `logs/m1-addon.log`: Add-on → Log tab, or `ha apps logs <slug>` (older CLI: `ha addons logs`)
   - if the build failed: the Supervisor log (`ha supervisor logs`) instead

**Done 2026-11-13**: all six checks passed; screenshot, diagnostics bundle and add-on log are in
`owner-reports/`.

What this settled: [VERIFIED-ON-HW] image build on the Pi, ingress path handling, SSE through
ingress, the `supervisor/core` REST and WebSocket proxy with `SUPERVISOR_TOKEN`, registry shapes on
your HA version.

## M2 — Jetson (scripts arrive in the next milestone; target: JetPack 6.x / L4T R36.5, 192.168.1.215)

Planned commands, so you know what is coming; do not run anything yet:

```bash
./jetson/setup.sh --check --json > owner-reports/setup/$(date +%F)_check.json
```

```bash
./jetson/setup.sh
```

```bash
./jetson/bench/run_all.sh
```

Things only the Jetson can answer: MAXN_SUPER mode id, tegrastats line format, real tok/s and
time-to-first-token, peak RAM and swap, thermals over a one-hour soak, tool-call accuracy of each
profile, whether the 4B GGUF file name in `models.md` resolves.

## Unverified facts to watch for (from RESEARCH.md)

- `repository.yaml` fields and icon sizes: the store listing on your HA settles these.
- llama-server `timings` field names, default port, `--mlock` spelling: `setup.sh --check` will
  record `llama-server --help` and one sample response.
