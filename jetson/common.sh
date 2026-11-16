#!/usr/bin/env bash
# Shared paths and helpers for the Jetson scripts. Sourced, never executed.
# AH_SYSROOT lets the tests point the read-only probes at a fixture tree instead of /.

AH_JETSON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AH_REPO_DIR="$(cd "$AH_JETSON_DIR/.." && pwd)"
AH_SYSROOT="${AH_SYSROOT:-}"
AH_ETC="${AH_ETC:-/etc/agent-house}"
AH_STATE="${AH_STATE:-/var/lib/agent-house}"
AH_LEARNER_UNIT="agent-house-learner.service"
# Units of the local model, removed in 0.5.0 (D40); `setup_learner.sh --stop-llm` disables them.
AH_OLD_UNITS="llama-server.service agent-house-metrics.service"
export AH_JETSON_DIR AH_REPO_DIR AH_SYSROOT AH_ETC AH_STATE AH_LEARNER_UNIT AH_OLD_UNITS

# Written by setup.sh: AH_USER.
if [[ -r "$AH_ETC/config.env" ]]; then
  # shellcheck source=/dev/null
  . "$AH_ETC/config.env"
fi

have() { command -v "$1" >/dev/null 2>&1; }
