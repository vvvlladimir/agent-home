# owner-reports

Results from the real hardware. The cloud sessions cannot reach your Pi, Jetson or Home Assistant;
this folder is how facts get back. `make analyze-report` reads it and rewrites the "Measured
results" block of `docs/DECISIONS.md`.

| Folder | What | Produced by | Schema |
|---|---|---|---|
| `diagnostics/*.zip` | add-on diagnostics bundle | GUI → Settings & Logs → Download diagnostics bundle | `contracts/schemas/diagnostics_manifest.schema.json` |
| `setup/*.json` | Jetson facts, nothing changed | `jetson/setup.sh --check --json` (M2) | `contracts/schemas/setup_check.schema.json` |
| `bench/*.json` | one benchmark report per profile | `jetson/bench/run_all.sh` (M2) | `contracts/schemas/bench_report.schema.json` |
| `screenshots/` | anything visual | you | — |
| `logs/` | add-on or Supervisor logs, `tegrastats` captures | you | — |
| `answers.md` | answers to the questions in `docs/OWNER_CHECKLIST.md` | you | free text |

Bundles and reports contain no tokens or API keys. Logs you paste yourself might: skim them first.
