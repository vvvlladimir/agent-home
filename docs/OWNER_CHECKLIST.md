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
   - [x] **Models & Benchmarks**: four profiles, `qwen35-4b-balanced` marked active
   - [x] **Run benchmark** with *Quick* ticked finishes and shows a row with tok/s and tool calls
   - [x] **Activate** `qwen35-2b-fast`: state shows `warming`, then `ok` with the new model;
         activate `qwen35-4b-balanced` again afterwards
   - [x] Settings → Local model requests: change *Timeout* to 90, Save, restart the add-on: still 90
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

Known issue (seen 2026-11-13): `HTTPS is not supported … failed to download model from Hugging Face`
means llama.cpp was built without OpenSSL. Fixed in the scripts; on a Jetson set up before the fix:
`sudo apt install libssl-dev && ./jetson/build_llamacpp.sh --clean && sudo systemctl restart llama-server`.

If something breaks: `journalctl -u llama-server -n 100`, `journalctl -u agent-house-metrics -n 50`
into `owner-reports/logs/`.

## M3 — room agents in Shadow Mode

Update the add-on, keep the global mode on **Shadow**. About 30 minutes.

1. **Agents → New room agent** for *Bedroom*, then for *Livingroom* (Q3). Tick nothing in the entity
   list (the room's entities are picked automatically), add one state rule on the room's motion or
   presence sensor (`To: on`), Save. Check the **Prompt preview**: token estimate below the budget,
   nothing important under *Left out*.
2. **Wake** each agent once. **Runs & traces** shows a run `ok` + `shadow`; open it: context, llm,
   tool and final steps. `[UNVERIFIED-ON-HW]` latency and tokens with the real model.
3. Walk into each room: a run with wake source `event` appears within a few seconds (debounce 5 s).
4. **Replay** one run: a new run marked `replay`, nothing changes in the house. **Copy as test case**:
   paste the YAML into `owner-reports/m3/<room>-case.yaml` (if the clipboard is blocked, it downloads).
5. Kill switch from HA: Developer Tools → Events → fire `agent_house_kill_switch` with
   `engaged: true`; the top bar turns red. Release it in the panel. Optional: create an
   `input_boolean.agent_house_kill`, set it in Settings as the kill-switch entity, toggle it in HA:
   the panel follows.
6. Leave both agents in Shadow for a day, then send `owner-reports/m3/` with: a screenshot of
   Runs & traces, a diagnostics bundle, and the case files. Note any run that would have actuated
   something you did not want (verdict `shadow` in the Action log).
7. Optional live test, one T0 light only: set that agent's mode to *live* and the global mode to
   *Live*, wake it with a reason like "turn on the lamp". The Action log shows `executed`. Back to
   Shadow afterwards. A T2 entity (blinds, climate) instead shows a pending approval on the
   Dashboard; Approve or Deny it there.

**Status 2026-11-13:** received (one hour, `owner-reports/m3/README.md`). Still open: leave both agents
in Shadow for a full day after updating, and check the Jetson metrics endpoint, which dropped the
connection in the diagnostics: `curl -s http://192.168.1.215:9100/metrics.json | head -c 300` and, if it
fails, `journalctl -u agent-house-metrics -n 50` (since 0.1.0-dev.7 the error is in the answer).

The Live feed shows *streaming* through ingress (verified in M1); if it ever shows *polling every 3 s*,
SSE is being buffered and the feed falls back to polling — tell me.

## M4 — proposals and deployed automations

Update to 0.1.0-dev.7 (or later), keep the global mode on **Shadow**. About 30 minutes. Optional for
the cloud part: put an Anthropic API key into the add-on's Configuration tab (`anthropic_api_key`);
`claude-haiku-5-5` and `daily_budget_eur: 1.0` are the defaults.

1. **Proposals → Describe it**, room *Bedroom*: "Turn the bed lamp on at 20 % when the bed radar sees
   someone between 22:00 and 06:00". Press *Draft*. `[UNVERIFIED-ON-HW]` how long the local model
   takes and whether it passes the checks on the first try; open the *Drafting trace* link to see it.
2. Open the proposal: all four checks `ok` (yaml, schema, policy, ha_validate). The `ha_validate`
   check is Home Assistant's own `validate_config`: `[UNVERIFIED-ON-HW]` against 2026.11.1.
   Press **Simulate against history** (7 days): the count should match how often the radar fired.
3. Try a blocked one: *Write YAML* with an action on `lock.*` or an `event:` step → status `blocked`,
   *Approve* is disabled.
4. **Approve and deploy** the lamp proposal. In HA → Settings → Automations a new automation with the
   same alias appears; its id (*⋮ → Information*) starts with `agent_house_`. Your own automations
   are unchanged.
5. **Deployed automations**: untick *Enabled* → the automation is off in HA; tick it again. Edit the
   proposal idea (Describe it again with the same automation, or paste changed YAML with the same
   alias), approve it, then **Roll back to** v1: HA shows the first version again.
6. Engage the kill switch: *Approve*, *Roll back* and *Draft* answer "kill switch engaged". Release.
7. Cloud (only with an API key): set `orchestrator_provider: anthropic`, draft once. The dashboard's
   Orchestrator card shows the spend; the drafting trace's `context` step is what was sent before
   redaction. Then back to `auto`.
8. Send `owner-reports/m4/`: a screenshot of Proposals and of Deployed automations, a diagnostics
   bundle (it now contains `proposals.json` and `automations.json`), and one line per proposal on
   whether the YAML is what you wanted.

## Unverified facts to watch for (from RESEARCH.md)

- `repository.yaml` fields and icon sizes: the store listing on your HA settles these.
- llama-server `timings` field names, default port, `--mlock` spelling: `setup.sh --check` will
  record `llama-server --help` and one sample response.
