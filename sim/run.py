"""`python -m sim.run [--port 8099]`: fake HA + LLM + Anthropic + Jetson + ingress around the real app."""

from __future__ import annotations

import argparse
import os
import random
import sys
import threading
from pathlib import Path

import uvicorn

from sim import fake_anthropic, fake_ha, fake_ingress, fake_jetson, fake_llm
from sim.server import ServerThread

ROOT = Path(__file__).resolve().parent.parent


def life(ha: fake_ha.HAState, stop: threading.Event, every: float = 20.0) -> None:
    """Make the house move: motion blips and temperature drift."""
    rng = random.Random()
    while not stop.wait(every):
        if rng.random() < 0.6:
            eid = rng.choice(["binary_sensor.hallway_motion", "binary_sensor.living_room_motion"])
            ha.set_state(eid, "on")
            if stop.wait(5):
                return
            ha.set_state(eid, "off")
        else:
            eid = rng.choice(["sensor.living_room_temperature", "sensor.bedroom_temperature"])
            cur = ha.states[eid]
            ha.set_state(eid, f"{float(cur['state']) + rng.choice([-0.2, 0.2]):.1f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--quiet-house", action="store_true")
    args = ap.parse_args()

    env = {
        "AH_DATA_DIR": ".data",
        "AH_HA_URL": "http://127.0.0.1:8123",
        "AH_HA_TOKEN": "sim-token",
        "AH_ALLOWED_PEERS": "127.0.0.1",
        "AH_LLM_URL": "http://127.0.0.1:8080",
        "AH_JETSON_METRICS_URL": "http://127.0.0.1:9100",
        "AH_JETSON_TOKEN": "jetson-sim",
        "AH_ANTHROPIC_BASE_URL": "http://127.0.0.1:8081",
        "AH_ANTHROPIC_API_KEY": "sk-ant-sim",
    }
    for k, v in env.items():
        os.environ.setdefault(k, v)
    sys.path.insert(0, str(ROOT / "agent_house"))

    ha = fake_ha.HAState.default()
    llm = fake_llm.LLMSim()
    jetson = fake_jetson.JetsonSim(llm)
    servers = [
        ServerThread(fake_ha.create_app(ha), port=8123),
        ServerThread(fake_llm.create_app(llm), port=8080),
        ServerThread(fake_anthropic.create_app(), port=8081),
        ServerThread(fake_jetson.create_app(jetson), port=9100),
        ServerThread(fake_ingress.create_app(f"http://127.0.0.1:{args.port}"), port=args.port + 1),
    ]
    for s in servers:
        s.start()
    stop = threading.Event()
    if not args.quiet_house:
        threading.Thread(target=life, args=(ha, stop), daemon=True).start()

    from app.main import create_app  # noqa: PLC0415  (env must be set first)

    print(f"\n  Agent House sim ready. Open: http://127.0.0.1:{args.port + 1}/\n", flush=True)
    try:
        uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="info")
    finally:
        stop.set()
        for s in servers:
            s.stop()


if __name__ == "__main__":
    main()
