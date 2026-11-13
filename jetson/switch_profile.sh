#!/usr/bin/env bash
# Make <profile> the resident model: point active.env at it and restart llama-server.
# Called by the metrics endpoint (POST /profile/<name>) and by bench/run_all.sh.
#   switch_profile.sh <profile> [--no-restart]
set -euo pipefail

# shellcheck source=jetson/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

name="${1:-}"
if ! valid_profile_name "$name"; then
  echo "usage: switch_profile.sh <profile> [--no-restart]" >&2
  exit 2
fi
file="$AH_PROFILES_DIR/$name.env"
if [[ ! -f "$file" ]]; then
  echo "unknown profile: $name" >&2
  exit 3
fi

mkdir -p "$AH_STATE"
ln -sfn "$file" "$AH_STATE/active.env"

if [[ "${2:-}" != "--no-restart" ]]; then
  # setup.sh installs a sudoers rule for exactly this command.
  if [[ "$(id -u)" -eq 0 ]]; then
    /usr/bin/systemctl restart "$AH_LLM_UNIT"
  else
    sudo -n /usr/bin/systemctl restart "$AH_LLM_UNIT"
  fi
fi
echo "$name"
