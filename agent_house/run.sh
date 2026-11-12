#!/usr/bin/with-contenv sh
# shellcheck shell=sh
# Started by the base image's s6-overlay /init (config.yaml: `init: false`).
# The with-contenv shebang is required: s6 does not pass the container environment to CMD, and
# without it SUPERVISOR_TOKEN would be empty (verified against base-python:3.14-alpine3.24).
# Options are read by the app itself from /data/options.json, so this script needs no bashio.
set -eu

if [ -z "${SUPERVISOR_TOKEN:-}" ]; then
    echo "[agent-house] WARNING: SUPERVISOR_TOKEN is not set; Home Assistant will be unreachable." >&2
fi

cd /opt/agent_house
# --host 0.0.0.0: the Supervisor ingress proxy connects from 172.30.32.2. The app itself rejects
# every other peer (app/main.py PeerGuard), so the port is not usable from the LAN even if published.
exec python3 -m uvicorn app.main:create_app --factory \
    --host 0.0.0.0 --port "${AH_PORT:-8099}" \
    --no-access-log --log-level warning
