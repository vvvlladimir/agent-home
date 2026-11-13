#!/usr/bin/env bash
# Benchmark every profile on this Jetson, one after the other, and write one report per profile
# to owner-reports/bench/. Restores the profile that was active before.
#
#   ./run_all.sh [--quick] [--soak MINUTES] [--include-experimental] [--profiles "a b"] [--no-pp0]
#
#   --quick                 a few minutes per profile instead of the full eval (smoke test)
#   --soak MINUTES          after the default profile's benchmark, generate for MINUTES and
#                           record temperature, throttling and the decode rate (use 60 once)
#   --include-experimental  also run qwen35-9b-experimental (little RAM headroom)
#   --no-pp0                skip the extra run of the default profile with presence penalty 0
#
# The first start of a profile downloads its GGUF (a few GB); that wait is not part of the
# measured cold-load time, the server is restarted once more for that.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=jetson/common.sh
. "$HERE/../common.sh"

QUICK=""
SOAK=0
EXPERIMENTAL=0
PP0=1
PROFILES="qwen35-4b-balanced qwen35-2b-fast gemma4-e2b-compare"
DOWNLOAD_TIMEOUT=3600
LOAD_TIMEOUT=600

while (($#)); do
  case "$1" in
    --quick) QUICK="--quick" ;;
    --soak) SOAK="${2:?--soak needs minutes}"; shift ;;
    --include-experimental) EXPERIMENTAL=1 ;;
    --profiles) PROFILES="${2:?--profiles needs a list}"; shift ;;
    --no-pp0) PP0=0 ;;
    -h | --help)
      sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      exit 2
      ;;
  esac
  shift
done
if ((EXPERIMENTAL)); then PROFILES="$PROFILES qwen35-9b-experimental"; fi

OUT="$AH_REPO_DIR/owner-reports/bench"
LOGS="$AH_REPO_DIR/owner-reports/logs"
TODAY="$(date +%F)"
mkdir -p "$OUT" "$LOGS"

BUILD=""
if [[ -r "$(dirname "$LLAMA_SERVER_BIN")/agent-house-build.txt" ]]; then
  BUILD="$(cat "$(dirname "$LLAMA_SERVER_BIN")/agent-house-build.txt")"
fi

# Raw material for the facts RESEARCH.md could not verify: flags of this build, tegrastats layout.
"$LLAMA_SERVER_BIN" --help >"$LOGS/${TODAY}_llama-server-help.txt" 2>&1 || true
if have tegrastats; then
  (timeout 5 tegrastats --interval 1000 || true) 2>&1 | head -n 3 >"$LOGS/${TODAY}_tegrastats.txt"
fi

ORIGINAL="$(active_profile)"
restore() {
  echo "restoring profile $ORIGINAL" >&2
  "$AH_JETSON_DIR/switch_profile.sh" "$ORIGINAL" >/dev/null || true
}
trap restore EXIT

FAILED=""
FIRST=1
for profile in $PROFILES; do
  echo "" >&2
  echo "=== $profile ===" >&2
  if ! "$AH_JETSON_DIR/switch_profile.sh" "$profile" >/dev/null; then
    FAILED="$FAILED $profile"
    continue
  fi
  echo "waiting for the model (the first start downloads it)…" >&2
  if ! wait_healthy "$DOWNLOAD_TIMEOUT" >/dev/null; then
    echo "$profile did not become healthy; see: journalctl -u $AH_LLM_UNIT -n 50" >&2
    journalctl -u "$AH_LLM_UNIT" -n 50 --no-pager >"$LOGS/${TODAY}_${profile}_failed-start.txt" 2>&1 || true
    FAILED="$FAILED $profile"
    continue
  fi
  # now the file is on disk: restart once more and time the load itself
  "$AH_JETSON_DIR/switch_profile.sh" "$profile" >/dev/null
  cold="$(wait_healthy "$LOAD_TIMEOUT" || echo "")"
  cold_args=()
  if [[ -n "$cold" ]]; then cold_args=(--cold-load-s "$cold"); fi

  soak_args=()
  if ((FIRST)) && [[ "$SOAK" != 0 ]]; then soak_args=(--soak-minutes "$SOAK"); fi
  # shellcheck disable=SC2086  # $QUICK is one flag or empty
  if ! python3 "$HERE/bench.py" --profile "$profile" --out "$OUT" $QUICK \
    --llama-build "$BUILD" --flags "$("$AH_JETSON_DIR/run_llama_server.sh" --print-cmd)" \
    --save-sample "$LOGS/${TODAY}_${profile}_sample-response.txt" \
    ${cold_args[@]+"${cold_args[@]}"} ${soak_args[@]+"${soak_args[@]}"}; then
    FAILED="$FAILED $profile"
  fi
  if ((FIRST && PP0)); then
    echo "--- $profile with presence penalty 0 ---" >&2
    # shellcheck disable=SC2086
    python3 "$HERE/bench.py" --profile "$profile@pp0" --out "$OUT" $QUICK \
      --llama-build "$BUILD" --flags "$("$AH_JETSON_DIR/run_llama_server.sh" --print-cmd)" \
      ${cold_args[@]+"${cold_args[@]}"} || FAILED="$FAILED $profile@pp0"
  fi
  FIRST=0
done

echo "" >&2
if [[ -n "$FAILED" ]]; then
  echo "failed:$FAILED" >&2
fi
echo "Reports are in $OUT. Commit them together with owner-reports/logs/${TODAY}_*:" >&2
echo "    git add owner-reports && git commit -m 'owner: M2 bench reports' && git push" >&2
[[ -z "$FAILED" ]]
