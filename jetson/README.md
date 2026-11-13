# jetson/

Runs on the Jetson Orin Nano, **not** inside the add-on. Step-by-step for the owner:
`docs/OWNER_CHECKLIST.md` → M2.

| File | What |
|---|---|
| `setup.sh` | power mode, clocks, swap, headless, packages, token, systemd units; `--check [--json]` changes nothing, `--dry-run` prints every command |
| `build_llamacpp.sh` | native CUDA build of `llama-server` for sm_87 (`--dry-run`) |
| `profiles/*.env`, `models.md` | one file per model profile; which models and why |
| `run_llama_server.sh`, `llama-server.service` | the server's command line from the active profile (`--print-cmd`) |
| `switch_profile.sh` | point `/var/lib/agent-house/active.env` at a profile and restart the server |
| `metrics/server.py`, `agent-house-metrics.service` | tegrastats → `GET /metrics.json` (`contracts/schemas/jetson_metrics.schema.json`); `POST /profile/<name>` with the bearer token |
| `bench/bench.py`, `bench/run_all.sh` | one report per profile → `owner-reports/bench/*.json` (`contracts/schemas/bench_report.schema.json`) |
| `bench/toolcall_eval.yaml` | link to the eval shared with the add-on (`agent_house/app/core/toolcall_eval.yaml`) |

Tested in CI against a fake sysroot, the fake llama-server and the fake Jetson
(`tests/unit/test_jetson_files.py`); nothing here has run on hardware yet [UNVERIFIED-ON-HW].
