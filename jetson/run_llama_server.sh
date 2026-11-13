#!/usr/bin/env bash
# ExecStart of llama-server.service: build the command line from the active profile and exec it.
#   run_llama_server.sh              start the server
#   run_llama_server.sh --print-cmd  print the command line and exit (used by the bench report)
set -euo pipefail

# shellcheck source=jetson/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

profile_file="${AH_PROFILE_FILE:-$(active_profile_file)}"
if [[ ! -r "$profile_file" ]]; then
  echo "profile file not found: $profile_file" >&2
  exit 1
fi
# shellcheck source=jetson/profiles/qwen35-4b-balanced.env
. "$profile_file"

args=(
  --host "$AH_LLM_HOST" --port "$AH_LLM_PORT"
  -hf "${MODEL_HF:?MODEL_HF missing in $profile_file}"
  --alias "${PROFILE_NAME:?PROFILE_NAME missing in $profile_file}"
  -c "${CTX_SIZE:-8192}"
  -ngl "${N_GPU_LAYERS:-99}"
  -np "${PARALLEL:-1}"
  -fa "${FLASH_ATTN:-on}"
  -ctk "${CACHE_TYPE_K:-q8_0}" -ctv "${CACHE_TYPE_V:-q8_0}"
  --metrics
)
if [[ "${THINKING:-off}" == "off" ]]; then
  args+=(--reasoning-budget 0)
fi
if [[ -n "${CHAT_TEMPLATE_KWARGS:-}" ]]; then
  args+=(--chat-template-kwargs "$CHAT_TEMPLATE_KWARGS")
fi
# One token for both Jetson services: the add-on sends it as a bearer token.
if [[ -s "$AH_ETC/token" ]]; then
  args+=(--api-key-file "$AH_ETC/token")
fi
# EXTRA_ARGS is split on whitespace on purpose (flags only, no quoting).
read -r -a extra <<<"${EXTRA_ARGS:-}"

if [[ "${1:-}" == "--print-cmd" ]]; then
  echo "llama-server ${args[*]} ${extra[*]+"${extra[*]}"}"
  exit 0
fi

if [[ ! -x "$LLAMA_SERVER_BIN" ]]; then
  echo "llama-server not found at $LLAMA_SERVER_BIN: run jetson/build_llamacpp.sh" >&2
  exit 1
fi
export LLAMA_CACHE="${LLAMA_CACHE:-$AH_STATE/models}"
exec "$LLAMA_SERVER_BIN" "${args[@]}" ${extra[@]+"${extra[@]}"}
