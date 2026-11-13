# Models

One resident model on the Orin Nano 8 GB, served by `llama-server` with one slot (`-np 1`).
Each profile is two files kept in step by `tests/unit/test_jetson_files.py`:

- server side: `profiles/<name>.env` (GGUF, context, KV cache, thinking switch), read by
  `run_llama_server.sh`;
- request side: `agent_house/app/core/profiles.py` (sampling, output limit), used by the add-on and
  by `bench/bench.py`.

All profiles are **provisional** until `bench/run_all.sh` has run on your Jetson and
`make analyze-report` has written the measured table into `docs/DECISIONS.md`.

| Profile | GGUF (`-hf`) | Ctx | Sampling | Why it is here |
|---|---|---|---|---|
| `qwen35-4b-balanced` (default) | `unsloth/Qwen3.5-4B-GGUF:Q4_K_M` | 8192 | T 0.7, top_p 0.8, top_k 20, presence 1.5, thinking off | Only candidate with a third-party Orin Nano measurement: ~16 tok/s, 3.9 GB peak (RESEARCH §5) |
| `qwen35-2b-fast` | `unsloth/Qwen3.5-2B-GGUF:Q4_K_M` | 8192 | same as 4B | Fallback if the 4B misses the 8 s wake target |
| `gemma4-e2b-compare` | `unsloth/gemma-4-E2B-it-GGUF:Q4_K_M` | 8192 | T 1.0, top_p 0.95, top_k 64, presence 0, template default | Native function calling; no Jetson numbers published |
| `qwen35-9b-experimental` | `unsloth/Qwen3.5-9B-GGUF:Q4_K_M` | 4096 | same as 4B | Fills the 8 GB; only with `run_all.sh --include-experimental` |

Shared server flags: `-ngl 99 -fa on -ctk q8_0 -ctv q8_0 -np 1 --metrics`, plus
`--api-key-file /etc/agent-house/token` once `setup.sh` has created the token. Qwen profiles add
`--reasoning-budget 0 --chat-template-kwargs '{"enable_thinking":false}'`.

## Downloads

`llama-server -hf <repo>:<quant>` downloads the GGUF on the first start into
`/var/lib/agent-house/models` (`LLAMA_CACHE`). Expect 1.5–6 GB per model; `run_all.sh` waits up to
an hour for the first start and does not count that time as the cold load.

[UNVERIFIED-ON-HW] The `:Q4_K_M` tag must match a file in each Hugging Face repo. RESEARCH §5
confirmed only the 9B file name (`Qwen3.5-9B-UD-Q4_K_XL.gguf`, which is a different quant). If a start fails
with "no file found", look at the repo's file list and set `MODEL_HF` to an existing quant, e.g.
`unsloth/Qwen3.5-4B-GGUF:UD-Q4_K_XL`. Change the matching `model` in `profiles.py` too, or
the test above fails.

## Trying another quant or model

1. Copy a profile: `cp profiles/qwen35-4b-balanced.env profiles/<new-name>.env`, edit `PROFILE_NAME`,
   `MODEL_HF`, `CTX_SIZE`.
2. Add the request side to `PROFILES` in `agent_house/app/core/profiles.py` and the name to
   `sim/fake_jetson.py`; run `make test`.
3. On the Jetson: `./bench/run_all.sh --profiles "<new-name>"`.

Gemma 4 E2B: the vendor suggests 8-bit for E2B. For a like-for-like comparison the profile uses
Q4_K_M; `unsloth/gemma-4-E2B-it-GGUF:Q8_0` is worth one extra run if Q4 loses on tool calls.

## Presence penalty

Qwen's 1.5 is a chat recommendation. Whether it hurts JSON tool arguments is open: `run_all.sh`
benchmarks the default profile a second time as `<profile>@pp0`. If `@pp0` wins on tool calls, set
**Settings → Local model requests → Presence penalty** to 0 in the GUI (no restart needed).

## Rules

- One resident model. A second one only if the benchmark shows ≥ 1 GB free RAM and no swap use.
- Thinking stays off for room agents (latency). The orchestrator may get a thinking profile later.
- Default changes are recorded in `docs/DECISIONS.md` after `make analyze-report`, never silently.
