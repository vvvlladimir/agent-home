#!/usr/bin/env bash
# Shared paths and helpers for the Jetson scripts. Sourced, never executed.
# AH_SYSROOT lets the tests point the read-only probes at a fixture tree instead of /.

AH_JETSON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AH_REPO_DIR="$(cd "$AH_JETSON_DIR/.." && pwd)"
AH_SYSROOT="${AH_SYSROOT:-}"
AH_ETC="${AH_ETC:-/etc/agent-house}"
AH_STATE="${AH_STATE:-/var/lib/agent-house}"
AH_PROFILES_DIR="${AH_PROFILES_DIR:-$AH_JETSON_DIR/profiles}"
AH_DEFAULT_PROFILE="${AH_DEFAULT_PROFILE:-qwen35-4b-balanced}"
AH_LLM_UNIT="llama-server.service"
AH_METRICS_UNIT="agent-house-metrics.service"
export AH_JETSON_DIR AH_REPO_DIR AH_SYSROOT AH_ETC AH_STATE AH_PROFILES_DIR AH_DEFAULT_PROFILE
export AH_LLM_UNIT AH_METRICS_UNIT

# Written by setup.sh: AH_USER, LLAMA_SERVER_BIN, AH_LLM_HOST, AH_LLM_PORT, AH_METRICS_PORT.
if [[ -r "$AH_ETC/config.env" ]]; then
  # shellcheck source=/dev/null
  . "$AH_ETC/config.env"
fi
AH_LLM_HOST="${AH_LLM_HOST:-0.0.0.0}"
AH_LLM_PORT="${AH_LLM_PORT:-8080}"
AH_METRICS_PORT="${AH_METRICS_PORT:-9100}"
LLAMA_SERVER_BIN="${LLAMA_SERVER_BIN:-$HOME/llama.cpp/build/bin/llama-server}"
export AH_LLM_HOST AH_LLM_PORT AH_METRICS_PORT LLAMA_SERVER_BIN

have() { command -v "$1" >/dev/null 2>&1; }

valid_profile_name() { [[ "$1" =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]]; }

# Name of the profile llama-server is (or will be) started with.
active_profile() {
  local target
  target="$(readlink "$AH_STATE/active.env" 2>/dev/null || true)"
  if [[ -n "$target" ]]; then
    basename "$target" .env
  else
    echo "$AH_DEFAULT_PROFILE"
  fi
}

active_profile_file() {
  if [[ -e "$AH_STATE/active.env" ]]; then
    echo "$AH_STATE/active.env"
  else
    echo "$AH_PROFILES_DIR/$AH_DEFAULT_PROFILE.env"
  fi
}

# Seconds until llama-server answers /health with 200; prints the elapsed time. $1 = timeout in s.
wait_healthy() {
  local timeout="$1" started now
  started="$(date +%s)"
  while true; do
    if curl -fsS -m 3 -o /dev/null "http://127.0.0.1:$AH_LLM_PORT/health" 2>/dev/null; then
      now="$(date +%s)"
      echo "$((now - started))"
      return 0
    fi
    now="$(date +%s)"
    if ((now - started >= timeout)); then
      return 1
    fi
    sleep 1
  done
}
