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

## M2 — Jetson: set up, benchmark, pick the default model

Target: Orin Nano 8 GB, JetPack 6.x / L4T R36.5, at `192.168.1.215`. Run everything as your normal
user over SSH from a checkout of this repository (`git clone https://github.com/vvvlladimir/agent-home`,
then `git checkout claude/stoic-cray-1j91qm` while the work is on a branch). Expect 1–3 hours in
total, mostly the llama.cpp build and the model downloads.

1. **Check, nothing is changed:**
   ```bash
   mkdir -p owner-reports/setup
   ./jetson/setup.sh --check --json > owner-reports/setup/$(date +%F)_check.json
   ./jetson/setup.sh --check            # the same as a readable table
   ```
   A `FAIL` line stops setup; tell me what it says. `--dry-run` lists every command setup would run.
2. **Set up** (asks once, uses sudo; power mode, swap, packages, token, systemd units, llama.cpp build
   of 20–40 minutes):
   ```bash
   ./jetson/setup.sh                    # add --headless to free the desktop's RAM (needs a reboot)
   ```
   If `nvpmodel` asks for a reboot, reboot and run `./jetson/setup.sh` again. The last lines print
   the three add-on options; **the token is shown only once** (it stays in `/etc/agent-house/token`).
3. **Watch the first model download** (a few GB), until it says the server is listening:
   ```bash
   journalctl -u llama-server -f
   curl -s http://127.0.0.1:9100/metrics.json | python3 -m json.tool | head -40
   ```
4. **Benchmark every profile** (4B, 2B, Gemma 4 E2B and the 4B with presence penalty 0; about an
   hour plus downloads). Restores the active profile at the end:
   ```bash
   ./jetson/bench/run_all.sh --quick          # 10-minute smoke test first
   ./jetson/bench/run_all.sh --soak 60        # the real run, with a one-hour thermal soak
   ```
   Optional: `--include-experimental` adds the 9B (little RAM headroom, may swap).
5. **Connect the add-on** (Home Assistant → Settings → Add-ons → Agent House → Configuration):
   ```yaml
   jetson_llm_url: http://192.168.1.215:8080
   jetson_metrics_url: http://192.168.1.215:9100
   jetson_token: <the token from step 2>
   ```
   Restart the add-on, update it first if the store offers 0.1.0-dev.2. Then check and note:
   - [ ] Dashboard → Jetson card: inference server `ok`, the model name, memory and temperature
   - [ ] **Models & Benchmarks**: four profiles, `qwen35-4b-balanced` marked active
   - [ ] **Run benchmark** with *Quick* ticked finishes and shows a row with tok/s and tool calls
   - [ ] **Activate** `qwen35-2b-fast`: state shows `warming`, then `ok` with the new model;
         activate `qwen35-4b-balanced` again afterwards
   - [ ] Settings → Local model requests: change *Timeout* to 90, Save, restart the add-on: still 90
6. **Commit** and push:
   ```bash
   git add owner-reports/setup owner-reports/bench owner-reports/logs
   git commit -m "owner: M2 setup check and bench reports" && git push
   ```
   Plus `owner-reports/screenshots/m2-models.png` (Models & Benchmarks page) and a fresh diagnostics
   bundle in `owner-reports/diagnostics/`.
7. `make analyze-report` (or just start the next session): writes the measured table and a
   recommendation into `docs/DECISIONS.md`; the default profile is then decided there.

Things only the Jetson can answer, all captured by the files above: the MAXN_SUPER mode id, the
tegrastats line format (`logs/*_tegrastats.txt`), llama-server flags of your build
(`logs/*_llama-server-help.txt`), the wire format of a tool call (`logs/*_sample-response.txt`),
real tok/s and time to first token, peak RAM and swap, thermals over the soak, tool-call accuracy per
profile, and whether each `:Q4_K_M` file name resolves (`jetson/models.md` → Downloads).

If something breaks: `journalctl -u llama-server -n 100`, `journalctl -u agent-house-metrics -n 50`
into `owner-reports/logs/`.

## Unverified facts to watch for (from RESEARCH.md)

- `repository.yaml` fields and icon sizes: the store listing on your HA settles these.
- llama-server `timings` field names, default port, `--mlock` spelling: `setup.sh --check` will
  record `llama-server --help` and one sample response.
