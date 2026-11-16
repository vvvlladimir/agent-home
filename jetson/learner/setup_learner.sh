#!/usr/bin/env bash
# Install the learner (D38) on the Jetson: a Python 3.12 venv, the systemd unit, the data directory.
#
#   ./setup_learner.sh [--data DIR] [--stop-llm] [--yes]
#   ./setup_learner.sh --dry-run     print every command that would run, change nothing
#
# --data DIR    where the journal lives (default /var/lib/agent-house/learner); put it on the NVMe,
#               e.g. --data /mnt/nvme/agent-house-learner
# --stop-llm    stop and disable llama-server and the metrics service of an install from before
#               0.5.0 (D40: the Jetson no longer serves a model; frees ~4 GB)
#
# Run jetson/setup.sh first (it writes the token the add-on and the learner share). Python 3.12 comes
# from uv (JetPack 6 ships 3.10; the learner imports the add-on's code, which needs 3.12). Idempotent.
# Written without the hardware [UNVERIFIED-ON-HW].
set -euo pipefail

# shellcheck source=jetson/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/../common.sh"

MODE=apply
YES=0
STOP_LLM=0
DATA="${AH_LEARNER_DATA:-$AH_STATE/learner}"
UNIT="$AH_LEARNER_UNIT"
PORT="${AH_LEARNER_PORT:-9200}"
RUN_USER="${AH_USER:-$(id -un)}"

usage() { sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while (($#)); do
  case "$1" in
    --dry-run) MODE=dry-run ;;
    --yes | -y) YES=1 ;;
    --stop-llm) STOP_LLM=1 ;;
    --data)
      DATA="${2:?--data needs a directory}"
      shift
      ;;
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

log() { echo "$*" >&2; }
run() {
  if [[ "$MODE" == dry-run ]]; then
    echo "would run: $*"
  else
    log "+ $*"
    "$@"
  fi
}

if [[ ! -r "$AH_ETC/token" ]]; then
  log "no token in $AH_ETC/token: run jetson/setup.sh first (the add-on and the learner share it)"
  [[ "$MODE" == dry-run ]] || exit 1
fi

UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
log "learner: data $DATA, user $RUN_USER, port $PORT, repository $AH_REPO_DIR"
if [[ "$MODE" == apply && "$YES" -eq 0 ]]; then
  read -r -p "Install the learner? [y/N] " answer
  if [[ ! "$answer" =~ ^[Yy] ]]; then
    log "Nothing changed."
    exit 0
  fi
fi

if [[ ! -x "$UV" ]]; then
  # the official installer, into ~/.local/bin (no root)
  if [[ "$MODE" == dry-run ]]; then
    echo "would run: curl -LsSf https://astral.sh/uv/install.sh | sh"
  else
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
fi

run sudo mkdir -p "$DATA"
run sudo chown "$RUN_USER" "$DATA"
if [[ ! -x "$DATA/venv/bin/python" ]]; then
  run "$UV" venv --python 3.12 "$DATA/venv"
fi
run "$UV" pip install --python "$DATA/venv/bin/python" -r "$AH_JETSON_DIR/learner/requirements.txt"

if [[ "$MODE" == dry-run ]]; then
  echo "would write: /etc/systemd/system/$UNIT (User=$RUN_USER, data $DATA)"
else
  sed -e "s|@USER@|$RUN_USER|g" -e "s|@REPO@|$AH_REPO_DIR|g" -e "s|@DATA@|$DATA|g" "$AH_JETSON_DIR/$UNIT" |
    sudo tee "/etc/systemd/system/$UNIT" >/dev/null
fi
run sudo systemctl daemon-reload
run sudo systemctl enable "$UNIT"
run sudo systemctl restart "$UNIT"

if ((STOP_LLM)); then
  for unit in $AH_OLD_UNITS; do
    run sudo systemctl disable --now "$unit" || true
  done
fi

if [[ "$MODE" == dry-run ]]; then
  echo "dry run: nothing was changed"
  exit 0
fi

address="$(hostname -I 2>/dev/null | awk '{ print $1 }')"
log ""
log "Done. Watch it with:  journalctl -u $UNIT -f"
log "Add-on configuration (Home Assistant → Settings → Add-ons → Agent House → Configuration):"
log "    jetson_learner_url: http://${address:-<jetson-ip>}:$PORT"
log "    jetson_token:       the content of $AH_ETC/token (written by jetson/setup.sh)"
