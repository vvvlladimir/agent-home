# Owner checklist

Everything here needs your hardware. Nothing below has been verified by the cloud session; each
step says what to commit so the next session can read the result.

Where results go: `owner-reports/` (see its README). After committing, run `make analyze-report`
or just start the next session.

## Questions (answer in `owner-reports/answers.md`, any format)

| # | Question | Default used until answered |
|---|---|---|
| Q1 | Supervisor and HA Core versions? (`ha info`, or Settings → About) | Supervisor ≥ 2026.04, Core 2026.x |
| Q2 | JetPack version on the Jetson? (`cat /etc/nv_tegra_release`) | detected at runtime; scripts branch on 6.x / 7.x |
| Q3 | Which rooms first, and 5–20 entities for each? | two sample rooms from your area registry |
| Q4 | Any perimeter entity not named like its domain (a garage door on a `switch`, a gate on a `cover`)? | only `cover.*garage*` is treated as T3 on top of the T3 domains |
| Q5 | Can you export your Node-RED flows as JSON? | importer reads `/share/agent_house/nodered/*.json` if present |
| Q6 | Daily Claude budget, and which model? | €1 / day, `claude-opus-5-5` (cheaper: `claude-sonnet-5-5`, `claude-haiku-5-5`) |
| Q7 | Static IP or DNS name of the Jetson? (`.local` names usually do not resolve inside add-ons) | you enter the URL in the add-on options |

## M1 — install the skeleton

Before starting: this build is read-only. It cannot switch anything in your home.

1. Home Assistant → Settings → Add-ons → Add-on Store → ⋮ → Repositories → add
   `https://github.com/vvvlladimir/agent-home` (while the work is on a branch, append
   `#claude/stoic-cray-1j91qm`).
2. Install **Agent House**. The Supervisor builds the image on the Pi; expect a few minutes.
3. Start it and open **Agent House** in the sidebar.
4. Check and note:
   - [ ] the page renders with styling (if it is unstyled, a URL is not relative: tell me)
   - [ ] the "HA" pill is green and shows your Core version
   - [ ] Dashboard → Rooms lists your areas with plausible entity counts
   - [ ] Agents page: locks, alarm panel, garage door show **T3**; lights show **T0**
   - [ ] Live feed shows state changes within a second or two of them happening (if they arrive in
         bursts or not at all, SSE is being buffered by ingress: tell me)
   - [ ] the kill switch toggles and survives an add-on restart
5. Commit to `owner-reports/`:
   - `screenshots/m1-dashboard.png`
   - `diagnostics/<date>.zip` from Settings & Logs → Download diagnostics bundle
   - `logs/m1-addon.log`: Add-on → Log tab, or `ha apps logs <slug>` (older CLI: `ha addons logs`)
   - if the build failed: the Supervisor log (`ha supervisor logs`) instead

What this settles: [UNVERIFIED-ON-HW] image build on the Pi, ingress path handling, SSE through
ingress, the `supervisor/core` REST and WebSocket proxy with `SUPERVISOR_TOKEN`, registry shapes on
your HA version.

## M2 — Jetson (scripts arrive in the next milestone)

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

- `float(0,100)` option schema and `repository.yaml` fields: the app linter in CI and the store
  listing on your HA settle these.
- llama-server `timings` field names, default port, `--mlock` spelling: `setup.sh --check` will
  record `llama-server --help` and one sample response.
