# jetson/

Runs on the Jetson Orin Nano, **not** inside the add-on. Arrives in M2 (see `docs/PLAN.md`):

- `setup.sh`: power mode, clocks, swap, headless, build deps; `--check` changes nothing
- `build_llamacpp.sh`: native CUDA build for sm_87
- `profiles/*.env`, `llama-server.service`, `models.md`
- `metrics/`: tegrastats → HTTP (`contracts/schemas/jetson_metrics.schema.json`)
- `bench/`: one command → `owner-reports/bench/*.json` (`contracts/schemas/bench_report.schema.json`)

Nothing here has been run on hardware yet.
