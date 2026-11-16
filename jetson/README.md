# jetson/

Runs on the Jetson Orin Nano, **not** inside the add-on. Since 0.5.0 (D40) the Jetson serves no
model: it is the learner (D38). Step-by-step for the owner: `docs/OWNER_CHECKLIST.md` → D38 learner.

| File | What |
|---|---|
| `setup.sh` | power mode, packages, the token the add-on and the learner share; `--check [--json]` changes nothing, `--dry-run` prints every command |
| `learner/setup_learner.sh` | uv + Python 3.12 venv, the unit, `--data` on the NVMe, `--stop-llm` disables the llama-server of an older install (`--dry-run`) |
| `learner/server.py`, `learner/*.py`, `agent-house-learner.service` | the learner: journal, nightly training, live suggestions, replay (`contracts/learner.openapi.yaml`); imports the add-on's `core/predict.py` and `core/insights.py` |

Tested in CI against a fake sysroot and in-process (`tests/unit/test_jetson_files.py`,
`tests/unit/test_learner.py`, `tests/integration/test_learner_loop.py`); nothing here has run on
hardware yet [UNVERIFIED-ON-HW].
