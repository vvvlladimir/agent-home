#!/usr/bin/env bash
# Prepare a Jetson Orin Nano 8 GB to run the Agent House learner (D38): power mode, packages, the
# shared token. The learner itself is installed by learner/setup_learner.sh afterwards.
#
#   ./setup.sh --check [--json]   read-only report of what is there and what setup would change
#   ./setup.sh --dry-run          print every command that would run, change nothing
#   ./setup.sh [--yes]            apply (asks once before changing anything unless --yes)
#
# Run it as your normal user from a checkout of the repository; it calls sudo where needed.
# Idempotent: a second run only reports "ok". Written without access to the hardware
# [UNVERIFIED-ON-HW]: every probe tolerates a missing tool or file and says so in the report.
set -euo pipefail

# shellcheck source=jetson/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

MODE=apply
JSON=0
YES=0
MIN_DISK_GB=10
# curl: uv's installer (setup_learner.sh); python3: the token and the --json report
APT_PACKAGES="curl python3"

usage() { sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while (($#)); do
  case "$1" in
    --check) MODE=check ;;
    --dry-run) MODE=dry-run ;;
    --json) JSON=1 ;;
    --yes | -y) YES=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done
if ((JSON)) && [[ "$MODE" != check ]]; then
  echo "--json only makes sense with --check" >&2
  exit 2
fi

log() { echo "$*" >&2; }

# ── facts (read-only) ────────────────────────────────────────────────────────────────────────

sysfile() { cat "$AH_SYSROOT$1" 2>/dev/null | tr -d '\0' || true; }
meminfo_mb() { awk -v key="$1:" '$1 == key { printf "%d", $2 / 1024 }' "$AH_SYSROOT/proc/meminfo" 2>/dev/null || true; }

MODULE="$(sysfile /proc/device-tree/model)"
TEGRA_RELEASE="$(sysfile /etc/nv_tegra_release | head -n 1)"
L4T_VERSION=""
JETPACK_MAJOR=""
if [[ "$TEGRA_RELEASE" =~ R([0-9]+)\ \(release\),\ REVISION:\ ([0-9.]+) ]]; then
  L4T_VERSION="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}"
  case "${BASH_REMATCH[1]}" in
    35) JETPACK_MAJOR=5 ;;
    36) JETPACK_MAJOR=6 ;;
    3[89] | 4*) JETPACK_MAJOR=7 ;;
  esac
fi
UBUNTU="$(sed -n 's/^VERSION_ID="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$AH_SYSROOT/etc/os-release" 2>/dev/null | head -n 1 || true)"
RAM_TOTAL_MB="$(meminfo_mb MemTotal)"
RAM_AVAILABLE_MB="$(meminfo_mb MemAvailable)"
SWAP_TOTAL_MB="$(meminfo_mb SwapTotal)"

IS_ORIN_NANO_8GB=""
if [[ -n "$MODULE" ]]; then
  # 8 GB modules report roughly 7.4–7.7 GB; the 4 GB module about 3.5 GB.
  if [[ "$MODULE" == *"Orin Nano"* && "${RAM_TOTAL_MB:-0}" -ge 6500 ]]; then
    IS_ORIN_NANO_8GB=true
  else
    IS_ORIN_NANO_8GB=false
  fi
fi

ZRAM_ENABLED=""
if [[ -r "$AH_SYSROOT/proc/swaps" ]]; then
  ZRAM_ENABLED=false
  if grep -q zram "$AH_SYSROOT/proc/swaps"; then ZRAM_ENABLED=true; fi
fi

POWER_MODE=""
POWER_MODES=""   # space-separated id:name
TARGET_MODE_ID=""
TARGET_MODE_NAME=""
if have nvpmodel; then
  POWER_MODE="$(nvpmodel -q 2>/dev/null | sed -n 's/.*NV Power Mode: *\([^ ]*\).*/\1/p' | head -n 1 || true)"
  # The mode ids differ between releases (MAXN_SUPER is 0 on some, 2 on others): never hardcode.
  modes_raw="$( (nvpmodel -p --verbose 2>/dev/null || true; cat "$AH_SYSROOT/etc/nvpmodel.conf" 2>/dev/null || true) |
    sed -n 's/.*POWER_MODEL:\{0,1\} *ID=\([0-9]*\) *NAME=\([A-Za-z0-9_]*\).*/\1:\2/p' | sort -u)"
  POWER_MODES="$(echo "$modes_raw" | tr '\n' ' ' | sed 's/ *$//')"
  for wanted in MAXN_SUPER MAXN; do
    for pair in $POWER_MODES; do
      if [[ "${pair#*:}" == "$wanted" && -z "$TARGET_MODE_ID" ]]; then
        TARGET_MODE_ID="${pair%%:*}"
        TARGET_MODE_NAME="$wanted"
      fi
    done
  done
fi

DEFAULT_TARGET=""
if have systemctl; then
  DEFAULT_TARGET="$(systemctl get-default 2>/dev/null || true)"
fi

DISK_FREE_GB=""
if df -Pk "$HOME" >/dev/null 2>&1; then
  DISK_FREE_GB="$(df -Pk "$HOME" | awk 'NR == 2 { printf "%.1f", $4 / 1048576 }')"
fi

MISSING_PACKAGES=""
if have dpkg-query; then
  for package in $APT_PACKAGES; do
    if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q "install ok installed"; then
      MISSING_PACKAGES="$MISSING_PACKAGES $package"
    fi
  done
  MISSING_PACKAGES="${MISSING_PACKAGES# }"
fi

RUN_USER="${SUDO_USER:-${USER:-$(id -un)}}"

# ── checks: name<TAB>status<TAB>detail<TAB>would_change ──────────────────────────────────────

CHECKS=()
FAILED=0
add_check() {
  CHECKS+=("$1"$'\t'"$2"$'\t'"$3"$'\t'"${4:-}")
  if [[ "$2" == fail ]]; then FAILED=1; fi
}

if [[ "$IS_ORIN_NANO_8GB" == true ]]; then
  add_check platform ok "$MODULE, ${RAM_TOTAL_MB} MB RAM"
elif [[ -z "$MODULE" ]]; then
  add_check platform fail "not a Jetson: /proc/device-tree/model is missing"
else
  add_check platform warn "$MODULE with ${RAM_TOTAL_MB:-?} MB RAM; written for an Orin Nano 8 GB"
fi

case "$JETPACK_MAJOR" in
  6 | 7) add_check jetpack ok "L4T $L4T_VERSION (JetPack $JETPACK_MAJOR)" ;;
  5) add_check jetpack warn "L4T $L4T_VERSION (JetPack 5): untested" ;;
  *) add_check jetpack fail "could not read the L4T release from /etc/nv_tegra_release" ;;
esac

if ! have nvpmodel; then
  add_check power_mode skip "nvpmodel not found"
elif [[ -z "$TARGET_MODE_ID" ]]; then
  add_check power_mode warn "current: ${POWER_MODE:-unknown}; no MAXN mode among: ${POWER_MODES:-none}"
elif [[ "$POWER_MODE" == "$TARGET_MODE_NAME" ]]; then
  add_check power_mode ok "$POWER_MODE"
else
  add_check power_mode warn "current: ${POWER_MODE:-unknown}" "sudo nvpmodel -m $TARGET_MODE_ID ($TARGET_MODE_NAME)"
fi

if [[ -z "$DISK_FREE_GB" ]]; then
  add_check disk skip "could not read free space"
elif awk -v free="$DISK_FREE_GB" -v min="$MIN_DISK_GB" 'BEGIN { exit !(free >= min) }'; then
  add_check disk ok "${DISK_FREE_GB} GB free in $HOME"
else
  add_check disk warn "${DISK_FREE_GB} GB free in $HOME; the venv wants about 2 GB, the journal belongs on the NVMe"
fi

if ! have dpkg-query; then
  add_check packages skip "not a Debian system"
elif [[ -z "$MISSING_PACKAGES" ]]; then
  add_check packages ok "$APT_PACKAGES"
else
  add_check packages warn "missing: $MISSING_PACKAGES" "sudo apt-get install -y $MISSING_PACKAGES"
fi

NEED_TOKEN=0
if [[ -e "$AH_ETC/token" ]]; then
  add_check token ok "$AH_ETC/token exists"
else
  NEED_TOKEN=1
  add_check token warn "no token yet: the learner would refuse every call" \
    "generate $AH_ETC/token (mode 600) and print it once for the add-on's jetson_token option"
fi

installed="$AH_SYSROOT/etc/systemd/system/$AH_LEARNER_UNIT"
if [[ -f "$installed" ]]; then
  add_check "unit:$AH_LEARNER_UNIT" ok "installed (learner/setup_learner.sh)"
else
  add_check "unit:$AH_LEARNER_UNIT" warn "not installed" "nothing here: run learner/setup_learner.sh next"
fi

for unit in $AH_OLD_UNITS; do
  if [[ -f "$AH_SYSROOT/etc/systemd/system/$unit" ]]; then
    add_check "old:$unit" warn "still installed from before 0.5.0 (D40)" "nothing here: learner/setup_learner.sh --stop-llm disables it"
  fi
done

# ── report ───────────────────────────────────────────────────────────────────────────────────

print_json() {
  local line
  {
    printf '%s\n' "MODULE=$MODULE" "IS_ORIN=$IS_ORIN_NANO_8GB" "L4T=$L4T_VERSION" "JETPACK=$JETPACK_MAJOR" \
      "UBUNTU=$UBUNTU" "RAM_TOTAL=$RAM_TOTAL_MB" "RAM_AVAILABLE=$RAM_AVAILABLE_MB" \
      "SWAP_TOTAL=$SWAP_TOTAL_MB" "ZRAM=$ZRAM_ENABLED" "POWER_MODE=$POWER_MODE" "POWER_MODES=$POWER_MODES" \
      "DEFAULT_TARGET=$DEFAULT_TARGET" "DISK_FREE=$DISK_FREE_GB" "CREATED=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "--"
    for line in "${CHECKS[@]}"; do printf '%s\n' "$line"; done
  } | python3 -c '
import json, sys

facts, checks, in_checks = {}, [], False
for raw in sys.stdin.read().splitlines():
    if raw == "--" and not in_checks:
        in_checks = True
    elif in_checks:
        name, status, detail, change = (raw.split("\t") + ["", "", ""])[:4]
        check = {"name": name, "status": status, "detail": detail}
        if change:
            check["would_change"] = change
        checks.append(check)
    else:
        key, _, value = raw.partition("=")
        facts[key] = value.strip() or None

def number(key, kind=float):
    try:
        return kind(facts[key])
    except (TypeError, ValueError):
        return None

def flag(key):
    return {"true": True, "false": False}.get(facts[key])

print(json.dumps({
    "schema_version": 1,
    "kind": "setup_check",
    "created": facts["CREATED"],
    "module": facts["MODULE"],
    "is_orin_nano_8gb": flag("IS_ORIN"),
    "l4t_version": facts["L4T"],
    "jetpack_major": number("JETPACK", int),
    "ubuntu": facts["UBUNTU"],
    "ram_total_mb": number("RAM_TOTAL"),
    "ram_available_mb": number("RAM_AVAILABLE"),
    "swap_total_mb": number("SWAP_TOTAL"),
    "zram_enabled": flag("ZRAM"),
    "power_mode": facts["POWER_MODE"],
    "power_modes_available": (facts["POWER_MODES"] or "").split(),
    "default_target": facts["DEFAULT_TARGET"],
    "disk_free_gb": number("DISK_FREE"),
    "checks": checks,
}, indent=2))'
}

print_table() {
  local line name status detail change
  log ""
  log "Jetson: ${MODULE:-unknown} · L4T ${L4T_VERSION:-?} · JetPack ${JETPACK_MAJOR:-?}"
  log ""
  for line in "${CHECKS[@]}"; do
    IFS=$'\t' read -r name status detail change <<<"$line"
    log "$(printf '  %-5s %-28s %s' "$(echo "$status" | tr '[:lower:]' '[:upper:]')" "$name" "$detail")"
    if [[ -n "$change" ]]; then log "        → $change"; fi
  done
  log ""
}

if [[ "$MODE" == check ]]; then
  if ((JSON)); then print_json; else print_table; fi
  exit 0
fi

# ── apply / dry-run ──────────────────────────────────────────────────────────────────────────

print_table
if ((FAILED)); then
  log "A check failed (see FAIL above). Fix it first; nothing was changed."
  exit 1
fi
if [[ "$(id -u)" -eq 0 && "$MODE" == apply ]]; then
  log "Run setup.sh as your normal user (it calls sudo itself): the learner runs as that user."
  exit 1
fi

run() {
  if [[ "$MODE" == dry-run ]]; then
    echo "would run: $*"
  else
    log "+ $*"
    "$@"
  fi
}

if [[ "$MODE" == apply && "$YES" -eq 0 ]]; then
  read -r -p "Apply the changes marked with → above? [y/N] " answer
  if [[ ! "$answer" =~ ^[Yy] ]]; then
    log "Nothing changed."
    exit 0
  fi
fi

if [[ -n "$MISSING_PACKAGES" ]]; then
  run sudo apt-get update
  # shellcheck disable=SC2086  # a list of package names
  run sudo apt-get install -y $MISSING_PACKAGES
fi

if have nvpmodel && [[ -n "$TARGET_MODE_ID" && "$POWER_MODE" != "$TARGET_MODE_NAME" ]]; then
  # nvpmodel may ask to reboot for this mode; answer it, then run setup.sh again.
  run sudo nvpmodel -m "$TARGET_MODE_ID"
fi

run sudo mkdir -p "$AH_ETC" "$AH_STATE"
run sudo chown -R "$RUN_USER" "$AH_STATE"
if [[ "$MODE" == dry-run ]]; then
  echo "would write: $AH_ETC/config.env (AH_USER)"
else
  printf '%s\n' "# Written by jetson/setup.sh" "AH_USER=$RUN_USER" | sudo tee "$AH_ETC/config.env" >/dev/null
fi

if ((NEED_TOKEN)); then
  if [[ "$MODE" == dry-run ]]; then
    echo "would write: $AH_ETC/token (random, mode 600, owner $RUN_USER)"
  else
    token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    printf '%s\n' "$token" | sudo tee "$AH_ETC/token" >/dev/null
    sudo chown "$RUN_USER" "$AH_ETC/token"
    sudo chmod 600 "$AH_ETC/token"
    NEW_TOKEN="$token"
  fi
fi

if [[ "$MODE" == dry-run ]]; then
  echo "dry run: nothing was changed"
  exit 0
fi

log ""
if [[ -n "${NEW_TOKEN:-}" ]]; then
  log "Token for the add-on's jetson_token option: $NEW_TOKEN"
  log "(shown once; it is stored in $AH_ETC/token)"
else
  log "The add-on's jetson_token option: the content of $AH_ETC/token"
fi
log ""
log "Next: ./jetson/learner/setup_learner.sh --data <a directory on the NVMe>"
