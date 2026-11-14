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
in Shadow for a full day after updating. Jetson metrics: the crash was a thermal zone answering
EAGAIN (fixed); on the Jetson `git pull && sudo systemctl restart agent-house-metrics`, then
`curl -s http://192.168.1.215:9100/metrics.json | head -c 300` must print JSON.

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
7. Cloud (only with an API key): set `orchestrator_provider: anthropic` (or `openai` with
   `openai_api_key`; default model `gpt-6.1-sol`), draft once. The dashboard's
   Orchestrator card shows the spend; the drafting trace's `context` step is what was sent before
   redaction. Then back to `auto`.
8. Send `owner-reports/m4/`: a screenshot of Proposals and of Deployed automations, a diagnostics
   bundle (it now contains `proposals.json` and `automations.json`), and one line per proposal on
   whether the YAML is what you wanted.

## M5 — migration (importer, parity, disable original)

1. Copy your Node-RED exports (Node-RED → menu → Export → *all flows* or one tab → JSON) into
   `/share/agent_house/nodered/` (Samba or File editor add-on). The add-on reads `/share` read-only.
2. **Migration → Import / refresh**. Expect one row per HA automation, per UI script and per
   top-level group of each Node-RED tab. `[UNVERIFIED-ON-HW]`: the script config path
   (`/config/script/config/<object_id>`) and automations without an `id` (YAML-only) — those show
   "no readable config"; note how many.
3. Pick a simple HA automation → **Propose replacement**. A draft appears on Proposals; open the
   original's row again for the **parity** line (original / replacement / matched over 7 days).
4. Approve the replacement, then untick **Enabled** on the original: HA shows your automation as
   off (not changed, not deleted). Tick it again: it is on again.
5. Try one Node-RED group: the summary says what was read and how many logic nodes were not.
   The draft is a starting point; say in `owner-reports/m5/README.md` how close it was.
6. **Chat**: ask the orchestrator "which lights are on in the living room?" and "turn the desk lamp
   on when I sit down" (a proposal must appear); ask a room agent to switch something (in Shadow
   the answer shows `shadow: light.turn_on …`, nothing moves). Note the wait per answer on the Jetson.
7. **Settings → Config export**: download the YAML, check it has no token or key, import it back:
   the message lists the agents and that they were set to shadow.
8. **Habit finder** (`[UNVERIFIED-ON-HW]`: the logbook fields `context_user_id`,
   `context_event_type`, `context_domain`, `context_entity_id` and that a wall switch leaves them
   empty). After a week or more of normal use: **Proposals → Found in the history → Scan history**.
   The line under the button says how many changes were read and how many were by hand; if "by
   hand" is 0 although you switch things yourself, send a diagnostics bundle and one
   `GET /api/logbook/<start>` row of a light you switched. Each habit row names the device, the
   sensor or time and the count; **Draft** one: a proposal appears with `Evidence:` in its rationale.
   Note in `owner-reports/m5/README.md` which habits were real and which were noise (the thresholds
   `habit_days` / `habit_min_repeats` are in Settings).
9. **New GUI** (`[UNVERIFIED-ON-HW]`: tested only in Chromium against `make sim`). Open the panel in
   the HA sidebar on a computer and in the Companion app on the phone. Check: the six tabs scroll on
   the phone; hovering (computer) or tapping the ⓘ buttons (phone) shows a tooltip that is not cut
   off by the HA frame; **Stop all** stops at once and **Resume…** asks; dark mode follows HA's
   theme or the ◐ button. Pages moved (the steps above use the old names): Dashboard → **Overview**;
   Live feed, Runs & traces → **Activity**; Agents → **Rooms**; Council → **Rooms → Council**;
   Proposals, Deployed automations, Migration → **Automations** (Drafts, Deployed, Your automations);
   Models & Benchmarks, Settings & Logs → **Settings** (General, Models, Logs). The habit finder is
   **Automations → Drafts → Found in the history → Scan**. Screenshot the Overview on both and put them in `owner-reports/m5/`.

## M5 — agent council, daily review, feedback (about 30 minutes, then a few days of Shadow)

Update to 0.1.0, keep the global mode on **Shadow**.

1. **Council → Create the mode helper**. HA → Settings → Helpers shows *Agent House mode* with the
   options home / away / night / guests / work. `[UNVERIFIED-ON-HW]`: the WebSocket command
   `input_select/create` and that the entity id becomes `input_select.agent_house_mode`; if it gets
   another id, rename it in HA to that one.
2. **Ownership**: the card lists each agent with its number of entities; look up a light of the
   bedroom (owner: the bedroom agent) and `sun.sun` (owner: orchestrator). A warning under the card
   means two agents share a room: decide which one keeps it.
3. **Settings → Automatic house mode** on, night window e.g. `23:00-07:00`, orchestrator mode
   *shadow*. Leave the house (or set your `person.*` away in HA → Developer tools → States): the
   feed shows `orchestrator: house mode → away (shadow …)`, nothing changes in HA. With
   `orchestrator_mode: live` *and* the global mode Live it switches the helper; set it to `guests`
   by hand: it stays `guests`.
4. **Board**: switch the helper to `away` by hand. Every enabled room gets a task ("check that
   everything is off"); its answer appears on the board as a `report`. Note the wait per room on
   the Jetson.
5. **Daily review**: Council → *Run review now*. The inbox gets *Daily review: …*; the Council card
   shows the summary and how many proposals it drafted (at most 3, one per finding, never two for
   the same entity). Then leave it to run at 04:30 for a few days.
6. **Feedback**: after a deployed automation has run for some days, undo it by hand a few times
   (switch the light back within 10 minutes). The next scan lists an `undo` finding; its draft has
   `Evidence: undone by hand …` in the rationale. `[UNVERIFIED-ON-HW]`: the logbook tells our
   automation's change from your switch (`context_*` fields).
7. **Conflict**: if two of our automations fight over one light, the `conflict` proposal replaces
   one and lists the other under *supersedes*; approving it switches the other one off (tick it on
   again on Deployed automations to undo).
8. Send `owner-reports/m5/`: screenshots of Council and Proposals, a diagnostics bundle, and one
   line per drafted proposal (useful / noise).

## v0.1.0 — release

1. `make analyze-report` after the M2 benchmark (done 2026-11-13): keep `qwen35-4b-balanced` with
   `llm_presence_penalty: 0` for the room agents (D23).
2. Update the add-on to 0.1.0 from the repository, take an HA backup first (RUNBOOK → Upgrades).
3. Go through RUNBOOK → *Going from Shadow to Live* for one room only.

## Sim panel: real backends (optional, about 15 minutes)

Everything else in the panel is tested in CI. These paths need your network or your keys:

1. `make sim`, open `http://127.0.0.1:8090/` → **Models & keys**. Enter the Jetson's IP and its
   token (`/etc/agent-house/token` on the Jetson), Save, **Test**. Expected: `/health 200, metrics
   200, profile qwen35-4b-balanced`. `[UNVERIFIED-ON-HW]` the Mac reaching the Jetson on ports
   8080 and 9100.
2. Room agents → backend *real Jetson*. Run the scenario **arrive home**. In **Requests**, the
   `llm` rows should show `jetson` with real latencies (warm wake around 6–7 s, D23).
3. Optional, costs cents: save a Claude key, set the orchestrator's Claude to *real API*, and ask
   the add-on's chat for one draft. The **Requests** tab shows the model and tokens.
   `[UNVERIFIED-ON-HW]` the gateway's passthrough with a real key.
4. Optional: save a DeepInfra key and set room agents to *OpenAI-compatible*. Press **Test
   compatible** to check that `Qwen/Qwen3.5-4B` is listed under that exact id, then fix the model
   field if it is not. `[UNVERIFIED-ON-HW]` the model id and the provider's tool calling for this
   model.

Note anything odd in `owner-reports/sim-panel.md`.

## Unverified facts to watch for (from RESEARCH.md)

- `repository.yaml` fields and icon sizes: the store listing on your HA settles these.
- llama-server `timings` field names, default port, `--mlock` spelling: `setup.sh --check` will
  record `llama-server --help` and one sample response.
