#!/usr/bin/env bash
# Prepare a Jetson Orin Nano 8 GB to serve the local model for Agent House.
#
#   ./setup.sh --check [--json]   read-only report of what is there and what setup would change
#   ./setup.sh --dry-run          print every command that would run, change nothing
#   ./setup.sh [--yes]            apply (asks once before changing anything unless --yes)
#
# Options: --headless    also boot to multi-user.target (frees the RAM the desktop uses; reboot)
#          --skip-build  do not build llama.cpp (run build_llamacpp.sh yourself)
#          --no-swap     do not create a swap file
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
HEADLESS=0
SKIP_BUILD=0
NO_SWAP=0
SWAPFILE="/swapfile-agent-house"
SWAP_GB=8
MIN_DISK_GB=15
# libssl-dev: llama.cpp downloads models (-hf) over its own HTTPS client; without it the build
# silently has no HTTPS and every model download fails.
APT_PACKAGES="git cmake build-essential libssl-dev libcurl4-openssl-dev curl python3 python3-yaml"

usage() { sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while (($#)); do
  case "$1" in
    --check) MODE=check ;;
    --dry-run) MODE=dry-run ;;
    --json) JSON=1 ;;
    --yes | -y) YES=1 ;;
    --headless) HEADLESS=1 ;;
    --skip-build) SKIP_BUILD=1 ;;
    --no-swap) NO_SWAP=1 ;;
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

CUDA_VERSION=""
CUDA_HOME="$AH_SYSROOT/usr/local/cuda"
if [[ -r "$CUDA_HOME/version.json" ]]; then
  CUDA_VERSION="$(sed -n 's/.*"version" *: *"\([0-9.]*\)".*/\1/p' "$CUDA_HOME/version.json" | head -n 1)"
elif [[ -x "$CUDA_HOME/bin/nvcc" ]]; then
  CUDA_VERSION="$("$CUDA_HOME/bin/nvcc" --version 2>/dev/null | sed -n 's/.*release \([0-9.]*\).*/\1/p' | head -n 1)"
fi

ZRAM_ENABLED=""
DISK_SWAP_MB=0
if [[ -r "$AH_SYSROOT/proc/swaps" ]]; then
  ZRAM_ENABLED=false
  while read -r swap_name _ swap_kb _; do
    case "$swap_name" in
      Filename) ;;
      */zram*) ZRAM_ENABLED=true ;;
      *) DISK_SWAP_MB=$((DISK_SWAP_MB + swap_kb / 1024)) ;;
    esac
  done <"$AH_SYSROOT/proc/swaps"
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

LLAMA_PATH=""
LLAMA_VERSION=""
if [[ -x "$LLAMA_SERVER_BIN" ]]; then
  LLAMA_PATH="$LLAMA_SERVER_BIN"
  LLAMA_VERSION="$("$LLAMA_SERVER_BIN" --version 2>&1 | head -n 1 || true)"
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
  add_check platform warn "$MODULE with ${RAM_TOTAL_MB:-?} MB RAM; the profiles are sized for an Orin Nano 8 GB"
fi

case "$JETPACK_MAJOR" in
  6 | 7) add_check jetpack ok "L4T $L4T_VERSION (JetPack $JETPACK_MAJOR)" ;;
  5) add_check jetpack warn "L4T $L4T_VERSION (JetPack 5): untested, CUDA 11 may be too old for current llama.cpp" ;;
  *) add_check jetpack fail "could not read the L4T release from /etc/nv_tegra_release" ;;
esac

if [[ -n "$CUDA_VERSION" ]]; then
  add_check cuda ok "CUDA $CUDA_VERSION in /usr/local/cuda"
else
  add_check cuda fail "no CUDA toolkit in /usr/local/cuda" "install it yourself: sudo apt install nvidia-jetpack (large download); setup does not"
fi

if ! have nvpmodel; then
  add_check power_mode skip "nvpmodel not found"
elif [[ -z "$TARGET_MODE_ID" ]]; then
  add_check power_mode warn "current: ${POWER_MODE:-unknown}; no MAXN mode among: ${POWER_MODES:-none}"
elif [[ "$POWER_MODE" == "$TARGET_MODE_NAME" ]]; then
  add_check power_mode ok "$POWER_MODE"
else
  add_check power_mode warn "current: ${POWER_MODE:-unknown}" "sudo nvpmodel -m $TARGET_MODE_ID ($TARGET_MODE_NAME)"
fi

if [[ -x "$AH_SYSROOT/usr/bin/jetson_clocks" ]]; then
  add_check jetson_clocks ok "run by llama-server.service at every start"
else
  add_check jetson_clocks skip "/usr/bin/jetson_clocks not found; the service continues without it"
fi

NEED_SWAP=0
if [[ -z "$ZRAM_ENABLED" ]]; then
  add_check swap skip "/proc/swaps not readable"
elif ((DISK_SWAP_MB >= 4000)); then
  add_check swap ok "${DISK_SWAP_MB} MB on disk (zram: $ZRAM_ENABLED)"
elif ((NO_SWAP)); then
  add_check swap warn "${DISK_SWAP_MB} MB on disk (zram: $ZRAM_ENABLED); --no-swap given"
else
  NEED_SWAP=1
  add_check swap warn "${DISK_SWAP_MB} MB on disk (zram: $ZRAM_ENABLED); zram alone competes with the model for RAM" \
    "create ${SWAP_GB} GB $SWAPFILE and add it to /etc/fstab (zram is left as it is)"
fi

if [[ -z "$DEFAULT_TARGET" ]]; then
  add_check headless skip "systemctl not available"
elif [[ "$DEFAULT_TARGET" == multi-user.target ]]; then
  add_check headless ok "boots to multi-user.target (no desktop)"
elif ((HEADLESS)); then
  add_check headless warn "boots to $DEFAULT_TARGET" "sudo systemctl set-default multi-user.target (effective after a reboot)"
else
  add_check headless warn "boots to $DEFAULT_TARGET: the desktop keeps several hundred MB of RAM" "nothing unless you pass --headless"
fi

if [[ -z "$DISK_FREE_GB" ]]; then
  add_check disk skip "could not read free space"
elif awk -v free="$DISK_FREE_GB" -v min="$MIN_DISK_GB" 'BEGIN { exit !(free >= min) }'; then
  add_check disk ok "${DISK_FREE_GB} GB free in $HOME"
else
  add_check disk warn "${DISK_FREE_GB} GB free in $HOME; models, build and swap want about ${MIN_DISK_GB} GB"
fi

if ! have dpkg-query; then
  add_check packages skip "not a Debian system"
elif [[ -z "$MISSING_PACKAGES" ]]; then
  add_check packages ok "$APT_PACKAGES"
else
  add_check packages warn "missing: $MISSING_PACKAGES" "sudo apt-get install -y $MISSING_PACKAGES"
fi

NEED_BUILD=0
if [[ -n "$LLAMA_PATH" ]]; then
  add_check llama_server ok "$LLAMA_PATH ($LLAMA_VERSION)"
elif ((SKIP_BUILD)); then
  add_check llama_server warn "not found at $LLAMA_SERVER_BIN; --skip-build given"
else
  NEED_BUILD=1
  add_check llama_server warn "not found at $LLAMA_SERVER_BIN" "jetson/build_llamacpp.sh (CUDA build, 20–40 minutes)"
fi

NEED_TOKEN=0
if [[ -e "$AH_ETC/token" ]]; then
  add_check token ok "$AH_ETC/token exists"
else
  NEED_TOKEN=1
  add_check token warn "no token yet: the profile switch and llama-server would be open or refused" \
    "generate $AH_ETC/token (mode 600) and print it once for the add-on's jetson_token option"
fi

NEED_UNITS=0
for unit in "$AH_LLM_UNIT" "$AH_METRICS_UNIT"; do
  installed="$AH_SYSROOT/etc/systemd/system/$unit"
  if [[ -f "$installed" ]] && sed -e "s|@USER@|$RUN_USER|g" -e "s|@REPO@|$AH_REPO_DIR|g" "$AH_JETSON_DIR/$unit" | cmp -s - "$installed"; then
    add_check "unit:$unit" ok "installed and up to date"
  else
    NEED_UNITS=1
    add_check "unit:$unit" warn "not installed or out of date" "install to /etc/systemd/system, enable and start as user $RUN_USER"
  fi
done

SUDOERS_LINE="$RUN_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart $AH_LLM_UNIT"
NEED_SUDOERS=0
if [[ -f "$AH_SYSROOT/etc/sudoers.d/agent-house" ]]; then
  add_check sudoers ok "/etc/sudoers.d/agent-house exists"
else
  NEED_SUDOERS=1
  add_check sudoers warn "the metrics service cannot restart llama-server yet" "add /etc/sudoers.d/agent-house: $SUDOERS_LINE"
fi

# ── report ───────────────────────────────────────────────────────────────────────────────────

print_json() {
  local line
  {
    printf '%s\n' "MODULE=$MODULE" "IS_ORIN=$IS_ORIN_NANO_8GB" "L4T=$L4T_VERSION" "JETPACK=$JETPACK_MAJOR" \
      "UBUNTU=$UBUNTU" "CUDA=$CUDA_VERSION" "RAM_TOTAL=$RAM_TOTAL_MB" "RAM_AVAILABLE=$RAM_AVAILABLE_MB" \
      "SWAP_TOTAL=$SWAP_TOTAL_MB" "ZRAM=$ZRAM_ENABLED" "POWER_MODE=$POWER_MODE" "POWER_MODES=$POWER_MODES" \
      "DEFAULT_TARGET=$DEFAULT_TARGET" "DISK_FREE=$DISK_FREE_GB" "LLAMA_PATH=$LLAMA_PATH" \
      "LLAMA_VERSION=$LLAMA_VERSION" "CREATED=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
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
    "cuda_version": facts["CUDA"],
    "ram_total_mb": number("RAM_TOTAL"),
    "ram_available_mb": number("RAM_AVAILABLE"),
    "swap_total_mb": number("SWAP_TOTAL"),
    "zram_enabled": flag("ZRAM"),
    "power_mode": facts["POWER_MODE"],
    "power_modes_available": (facts["POWER_MODES"] or "").split(),
    "default_target": facts["DEFAULT_TARGET"],
    "disk_free_gb": number("DISK_FREE"),
    "llama_server_path": facts["LLAMA_PATH"],
    "llama_server_version": facts["LLAMA_VERSION"],
    "checks": checks,
}, indent=2))'
}

print_table() {
  local line name status detail change
  log ""
  log "Jetson: ${MODULE:-unknown} · L4T ${L4T_VERSION:-?} · JetPack ${JETPACK_MAJOR:-?} · CUDA ${CUDA_VERSION:-?}"
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
  log "Run setup.sh as your normal user (it calls sudo itself): the services run as that user."
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

if ((NEED_SWAP)); then
  if [[ ! -e "$SWAPFILE" ]]; then
    run sudo fallocate -l "${SWAP_GB}G" "$SWAPFILE"
    run sudo chmod 600 "$SWAPFILE"
    run sudo mkswap "$SWAPFILE"
  fi
  run sudo swapon "$SWAPFILE"
  if ! grep -q "^$SWAPFILE " /etc/fstab 2>/dev/null; then
    run sudo sh -c "echo '$SWAPFILE none swap sw,pri=-2 0 0' >> /etc/fstab"
  fi
fi

if ((HEADLESS)) && [[ -n "$DEFAULT_TARGET" && "$DEFAULT_TARGET" != multi-user.target ]]; then
  run sudo systemctl set-default multi-user.target
fi

run sudo mkdir -p "$AH_ETC" "$AH_STATE/models"
run sudo chown -R "$RUN_USER" "$AH_STATE"
if [[ "$MODE" == dry-run ]]; then
  echo "would write: $AH_ETC/config.env (AH_USER, LLAMA_SERVER_BIN, ports)"
else
  printf '%s\n' "# Written by jetson/setup.sh" "AH_USER=$RUN_USER" "LLAMA_SERVER_BIN=$LLAMA_SERVER_BIN" \
    "AH_LLM_HOST=$AH_LLM_HOST" "AH_LLM_PORT=$AH_LLM_PORT" "AH_METRICS_PORT=$AH_METRICS_PORT" |
    sudo tee "$AH_ETC/config.env" >/dev/null
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

if [[ ! -e "$AH_STATE/active.env" ]]; then
  run "$AH_JETSON_DIR/switch_profile.sh" "$AH_DEFAULT_PROFILE" --no-restart
fi

if ((NEED_BUILD)); then
  if [[ "$MODE" == dry-run ]]; then
    run "$AH_JETSON_DIR/build_llamacpp.sh" --dry-run
  else
    run "$AH_JETSON_DIR/build_llamacpp.sh"
  fi
fi

if ((NEED_SUDOERS)); then
  if [[ "$MODE" == dry-run ]]; then
    echo "would write: /etc/sudoers.d/agent-house: $SUDOERS_LINE"
  else
    sudoers_tmp="$(mktemp)"
    printf '%s\n' "$SUDOERS_LINE" >"$sudoers_tmp"
    # a broken sudoers file locks sudo: validate before installing
    sudo visudo -cf "$sudoers_tmp"
    sudo install -m 440 -o root -g root "$sudoers_tmp" /etc/sudoers.d/agent-house
    rm -f "$sudoers_tmp"
  fi
fi

if ((NEED_UNITS)); then
  for unit in "$AH_LLM_UNIT" "$AH_METRICS_UNIT"; do
    if [[ "$MODE" == dry-run ]]; then
      echo "would write: /etc/systemd/system/$unit (User=$RUN_USER, repository $AH_REPO_DIR)"
    else
      sed -e "s|@USER@|$RUN_USER|g" -e "s|@REPO@|$AH_REPO_DIR|g" "$AH_JETSON_DIR/$unit" |
        sudo tee "/etc/systemd/system/$unit" >/dev/null
    fi
  done
  run sudo systemctl daemon-reload
fi
run sudo systemctl enable "$AH_LLM_UNIT" "$AH_METRICS_UNIT"
run sudo systemctl restart "$AH_METRICS_UNIT"
if [[ "$MODE" == dry-run || -x "$LLAMA_SERVER_BIN" ]]; then
  run sudo systemctl restart "$AH_LLM_UNIT"
else
  log "llama-server is not built yet: run jetson/build_llamacpp.sh, then sudo systemctl restart $AH_LLM_UNIT"
fi

if [[ "$MODE" == dry-run ]]; then
  echo "dry run: nothing was changed"
  exit 0
fi

address="$(hostname -I 2>/dev/null | awk '{ print $1 }')"
log ""
log "Done. llama-server downloads the model on its first start (a few GB): watch it with"
log "    journalctl -u $AH_LLM_UNIT -f"
log ""
log "Add-on configuration (Home Assistant → Settings → Add-ons → Agent House → Configuration):"
log "    jetson_llm_url:     http://${address:-<this-jetson>}:$AH_LLM_PORT"
log "    jetson_metrics_url: http://${address:-<this-jetson>}:$AH_METRICS_PORT"
if [[ -n "${NEW_TOKEN:-}" ]]; then
  log "    jetson_token:       $NEW_TOKEN"
  log "(the token is shown once; it is stored in $AH_ETC/token)"
else
  log "    jetson_token:       the content of $AH_ETC/token"
fi
log ""
log "Next: ./jetson/bench/run_all.sh"
