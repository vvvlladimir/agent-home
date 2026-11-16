# Owner checklist

Everything here needs your hardware. Nothing below has been verified by the cloud session; each
step says what to commit so the next session can read the result.

Where results go: `owner-reports/` (see its README). After committing, start the next session.
Since 0.5.0 (D40) there are no room agents and no local model: the M2 steps (Jetson model,
benchmark) are gone, M3 is redone with the house manager, the learner (D38) replaces the rest.

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

## D40 — upgrade to 0.5.0 (about 15 minutes)

The room agents and the model on the Jetson are removed. Your benchmark of 2026-11-13 stays in
`owner-reports/bench/` (D23). Take an HA backup first (RUNBOOK → Upgrades).

1. Update the add-on to 0.5.0. `[UNVERIFIED-ON-HW]` the Supervisor accepts the old options: it drops
   `jetson_llm_url` / `jetson_metrics_url` (no longer in the schema) and keeps
   `orchestrator_provider: local` valid (read as `auto`). If the add-on refuses to start with
   "invalid options", open Configuration, save once, start again.
2. Settings: your GUI settings (habits, house mode, manager, learner mode) are still there; the model
   settings are gone. The top bar shows the learner instead of the model.
3. Configuration: set `orchestrator_provider` to `anthropic` or `openai` (or keep `auto`) with its
   key; without a key there are no drafts, chat or house manager runs (the daily review is then the
   digest alone).
4. On the Jetson: `git pull && jetson/learner/setup_learner.sh --stop-llm` (stops and disables
   `llama-server` and `agent-house-metrics`; frees about 4 GB). Then the D38 steps below if the
   learner is not installed yet.
5. Open `api/dev` (the Developer tools page once the GUI has it): no token or key appears; the
   learner section shows what the Jetson came up with. Send a diagnostics bundle if anything fails.

## M3 — the safety levers with the house manager (about 20 minutes, Shadow)

Keep the global mode on **Shadow**. The M3 run with room agents (2026-11-13, `owner-reports/m3/`)
is history; the policy, approvals, action log and kill switch it tested are unchanged.

1. Settings → *House manager* on. Leave a light on and set every `person.*` away (or leave): within
   the sweep interval the feed shows `manager: …`, Activity shows an action of agent
   `orchestrator` with verdict `shadow`. Nothing changes in the house.
2. Kill switch from HA: Developer Tools → Events → fire `agent_house_kill_switch` with
   `engaged: true`; the top bar turns red, *Ask the house manager* is refused. Release it in the
   panel. Optional: create an `input_boolean.agent_house_kill`, set it in Settings as the kill-switch
   entity, toggle it in HA: the panel follows.
3. Optional live test, one T0 light only: *Orchestrator mode* live and the global mode Live, leave
   that light on with nobody home: the Action log shows `executed`. A T2 entity (blinds, climate)
   instead shows a pending approval on the Overview; Approve or Deny it there. Back to Shadow.

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
   on when I sit down" (a proposal must appear). Note the wait per answer.
7. **Settings → Config export**: download the YAML, check it has no token or key, import it back:
   policy and settings are applied. An export from 0.4 imports too; its agents are listed as ignored.
8. **Habit finder** (`[UNVERIFIED-ON-HW]`: the logbook fields `context_user_id`,
   `context_event_type`, `context_domain`, `context_entity_id` and that a wall switch leaves them
   empty). After a week or more of normal use: **Proposals → Found in the history → Scan history**.
   The line under the button says how many changes were read and how many were by hand; if "by
   hand" is 0 although you switch things yourself, send a diagnostics bundle and one
   `GET /api/logbook/<start>` row of a light you switched. Each habit row names the device, the
   sensor or time and the count; **Draft** one: a proposal appears with `Evidence:` in its rationale.
   Note in `owner-reports/m5/README.md` which habits were real and which were noise (the thresholds
   `habit_days` / `habit_min_repeats` are in Settings).
   **Programs and your own automations** (`[UNVERIFIED-ON-HW]`): if you use Node-RED, AppDaemon or
   pyscript, switch a light from one of them and look at that logbook row: note which
   `context_user_id` it has and the name of that user under **HA Settings → People → Users**.
   Users named like Node-RED, AppDaemon or pyscript are recognised on their own only if HA lists
   its users to the add-on's token (`config/auth/list` needs an admin token); otherwise enter the
   user id in **Settings → Habits: HA users that are programs**. Then **Scan** again: the device
   must not show up as your habit, and a device your own automation mostly runs is counted under
   "the owner's own automation already does it". After **Your automations → Import**, a draft
   that acts on an entity one of yours also acts on names it in the `policy` check.
9. **New GUI** (`[UNVERIFIED-ON-HW]`: tested only in Chromium against `make sim`). Open the panel in
   the HA sidebar on a computer and in the Companion app on the phone. Check: the six tabs scroll on
   the phone; hovering (computer) or tapping the ⓘ buttons (phone) shows a tooltip that is not cut
   off by the HA frame; **Stop all** stops at once and **Resume…** asks; dark mode follows HA's
   theme or the ◐ button. Pages moved (the steps above use the old names): Dashboard → **Overview**;
   Live feed, Runs & traces → **Activity**; Council → **Rooms → Council**;
   Proposals, Deployed automations, Migration → **Automations** (Drafts, Deployed, Your automations);
   Settings & Logs → **Settings** (General, Logs). (The Agents and Models pages are gone in 0.5.0.) The habit finder is
   **Automations → Drafts → Found in the history → Scan**. Screenshot the Overview on both and put them in `owner-reports/m5/`.

## M5 — agent council, daily review, feedback (about 30 minutes, then a few days of Shadow)

Update to 0.1.0, keep the global mode on **Shadow**.

1. **Council → Create the mode helper**. HA → Settings → Helpers shows *Agent House mode* with the
   options home / away / night / guests / work. `[UNVERIFIED-ON-HW]`: the WebSocket command
   `input_select/create` and that the entity id becomes `input_select.agent_house_mode`; if it gets
   another id, rename it in HA to that one.
2. **Ownership** (0.5.0): every entity is the orchestrator's; network pairing switches
   (`*_permit_join`) belong to nobody, so nothing of ours can switch them.
3. **Settings → Automatic house mode** on, night window e.g. `23:00-07:00`, orchestrator mode
   *shadow*. Leave the house (or set your `person.*` away in HA → Developer tools → States): the
   feed shows `orchestrator: house mode → away (shadow …)`, nothing changes in HA. With
   `orchestrator_mode: live` *and* the global mode Live it switches the helper; set it to `guests`
   by hand: it stays `guests`.
4. **Daily review**: Council → *Run review now*. The inbox gets *Daily review: …*; the Council card
   shows the summary and how many proposals it drafted (at most 3, one per finding, never two for
   the same entity). Then leave it to run at 04:30 for a few days.
5. **Feedback**: after a deployed automation has run for some days, undo it by hand a few times
   (switch the light back within 10 minutes). The next scan lists an `undo` finding; its draft has
   `Evidence: undone by hand …` in the rationale. `[UNVERIFIED-ON-HW]`: the logbook tells our
   automation's change from your switch (`context_*` fields).
6. **Conflict**: if two of our automations fight over one light, the `conflict` proposal replaces
   one and lists the other under *supersedes*; approving it switches the other one off (tick it on
   again on Deployed automations to undo).
7. Send `owner-reports/m5/`: screenshots of Council and Proposals, a diagnostics bundle, and one
   line per drafted proposal (useful / noise).

## v0.1.0 — release

1. ~~`make analyze-report` after the M2 benchmark~~ (done 2026-11-13, D23; the tool is gone since D40).
2. Update the add-on to 0.1.0 from the repository, take an HA backup first (RUNBOOK → Upgrades).
3. Go through RUNBOOK → *Going from Shadow to Live* for one actor only (0.5.0: the learner or the
   house manager).

## Sim panel: real backends (optional, about 15 minutes)

Everything else in the panel is tested in CI. These paths need your network or your keys:

1. `make sim`, open `http://127.0.0.1:8090/` → **Models & keys**. The *Learner* card shows the
   learner in the sim (journal, training, live targets); press **Train now**: *training: ok* within
   a minute.
2. *Which learner*: the real Jetson by IP, port 9200, its token (`/etc/agent-house/token`), Save,
   **Test**. Expected: `learner /healthz 200, /status 200, journal … events`.
   `[UNVERIFIED-ON-HW]` the Mac reaching the Jetson on port 9200. The sim's house then goes to your
   real learner's journal: run that learner with a separate `--data` for it.
3. Optional, costs cents: save a Claude key, set the orchestrator's Claude to *real API*, and ask
   the add-on's chat for one draft. The **Requests** tab shows the model and tokens.
   `[UNVERIFIED-ON-HW]` the gateway's passthrough with a real key.
4. Polygon with a real model (costs cents with a cloud key): `export ANTHROPIC_API_KEY=…`, then
   `make polygon ARGS="--orchestrator anthropic"` (or `--learner jetson --jetson-host <ip>`).
   Expected: `{"ok": true, …}`. A failing safety check (`shadow_*`, `t3_*`, `owner_*`,
   `known_actors_only`) is a bug: report it with the report file.

Note anything odd in `owner-reports/sim-panel.md`.

## AI testers on Claude Code alone (about 1 hour, no API keys)

1. claude.ai/code → New session on `agent-home`, model Opus, and type `/polygon-claude`
   (optionally "roles all" or a list). Expected: it reports `models: claude`, spawns one
   subagent per role, then verifiers, and ends with the confirmed bugs and the backlog path.
2. Commit the copied `owner-reports/qa/<date>-claude.md`.
   `[UNVERIFIED-ON-HW]` in the cloud environment: `make install` reaches PyPI, the background
   polygons stay up while subagents run, and a nested `claude -p` is logged in (else the report
   says `models: fake`). Checked on a Mac only.

## AI testers on the polygon (about 1 hour, costs API tokens)

Needs the `claude` CLI logged in, `OPENAI_API_KEY` (orchestrator) and `AH_SIM_COMPATIBLE_API_KEY`
(DeepInfra, the coordinator). Details: `docs/POLYGON.md`.

1. `python -m sim.qa run --roles newcomer migrator fussy_tenant -- --orchestrator openai
   --orchestrator-model gpt-6.1-terra --backfill-days 14`.
   Expected: three URLs, then `{"out": ...}` when the testers finish.
2. `python -m sim.qa coordinate <out>`, then `python -m sim.qa verify <out>`.
   Expected: `<out>/backlog.md` with `confirmed` items first.
3. Check the OpenAI spend for the run in the add-on's Requests tab and on the DeepInfra dashboard.
4. Commit `backlog.md` to `owner-reports/qa/<date>.md`.
   `[UNVERIFIED-ON-HW]` the same roles with `--learner jetson --jetson-host <ip>` and one role only.

## D38 — the learner on the Jetson (about 30 minutes, then a week of watching)

What it does: the Jetson keeps the house's history on its own disk and learns from it every night
(habits over months, routines, what follows what, which automations you keep correcting). It never
switches anything itself and gets no Home Assistant token. Plan: `docs/PLAN.md` → D38; decisions D38, D39.

1. **Before the first start (optional):** in HA's `configuration.yaml`, `recorder: purge_keep_days: 60`,
   restart HA. The learner reads what HA still has on its first contact; after that it keeps its own.
2. **On the Jetson:** `git pull`, then (once) `jetson/setup.sh` if `/etc/agent-house/token` does not
   exist. Mount the NVMe, then
   `jetson/learner/setup_learner.sh --data /mnt/nvme/agent-house-learner` — add `--stop-llm` on a
   Jetson that ran llama-server (D40). Check: `journalctl -u agent-house-learner -f` and
   `curl -H "Authorization: Bearer $(cat /etc/agent-house/token)" http://localhost:9200/status`.
3. **Add-on options:** `jetson_learner_url: http://<jetson-ip>:9200`, `jetson_token` = the Jetson's token,
   and `orchestrator_provider` with a cloud key (drafts have no other model since 0.5.0). Restart the add-on.
4. **Council page → Learner:** *connected*; *From the logbook: done*; press *Train now* once; *Training: ok*
   within minutes. Send `owner-reports/` a screenshot or `GET api/learner` if anything says failed.
5. **After a week:** the card's *What it found* and the findings (a `timeout` finding names a light
   controller you keep correcting, with the timeout the history asks for). Activity → filter agent
   `learner`: every `shadow` action should be one you wanted. Only then Settings → *Learner mode: live*
   (the global mode must be live too).
6. **Backup (optional):** the journal is `<data>/journal.db` (SQLite, WAL): copy it weekly, or
   `sqlite3 journal.db ".backup /nas/learner.db"`.

Unverified until this runs on your hardware [UNVERIFIED-ON-HW]: an automation's actions carry the
context id of its `automation_triggered` event (the cause tags); uv installs Python 3.12 on the Orin;
scikit-learn's aarch64 wheel installs on your JetPack; the first backfill of a big house (day by day,
logbook only) finishes in minutes; a nightly training takes minutes on the Orin's CPU; port 9200 is
reachable from the Pi.

## Unverified facts to watch for (from RESEARCH.md)

- `repository.yaml` fields and icon sizes: the store listing on your HA settles these.
- `[UNVERIFIED-ON-HW]` The `t3_whitelist` add-on option (QA-004, a list of `match(...)` entries):
  open Settings → Apps → Agent House → Configuration, add `lock.front_door`, save, restart.
  Expected: the GUI's Settings page lists it under add-on options, the entity shows T2, and
  `PUT api/policy` with a different `t3_whitelist` answers 422.
- `[UNVERIFIED-ON-HW]` Habit predictions on the real house (D35, ~1900 entities): after install,
  wait for the inbox line "First look at your last 14 days" (or Proposals → Scan). Expected: the
  scan finishes within a minute or two; each proposal names a situation ("Hallway Motion is on and
  from 17:00"), not a clock time, with how often it was right on days it had not seen. Note in
  `owner-reports/` any proposal that would be wrong, and any device you would want that waits.
- `[UNVERIFIED-ON-HW]` Asks (D35): an "asks first" proposal uses `notify.notify` with an actionable
  button. Approve one, then check that the phone shows the button and that tapping it switches the
  device (the event is `mobile_app_notification_action` with the button's `action`). If your
  companion app needs `notify.mobile_app_<phone>` instead, edit the draft before approving.
  Then ignore one question and switch the device yourself: the notification should disappear
  (`clear_notification` with the same `tag`). In Settings → Logbook, check that the ask automation
  shows "triggered" entries (no state): the next scan counts them (D36).
- `[UNVERIFIED-ON-HW]` Unusual states (D36): after the first scan, leave a lamp on at night for
  30+ min; the inbox should get one "Unusual: …" note, and none for lamps on at usual times.
