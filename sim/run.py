"""`python -m sim.run`: the real add-on inside a simulated home, steered from the sim panel.

    :8100  the add-on behind a fake ingress (open this)     :8090  the sim panel
    :8123  fake Home Assistant                              :8099  the add-on itself
    :8080  room-agent LLM gateway   :8081 Claude gateway   :8082 OpenAI gateway   :9100 Jetson metrics

The gateways start on the fakes; the panel switches them to a real Jetson (by IP) or the real
OpenAI / Claude APIs with keys kept in `.data/sim/secrets.json`. See docs/RUNBOOK.md "Simulator".
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import uvicorn

from sim import control, fake_anthropic, fake_ha, fake_ingress, fake_jetson, fake_llm, fake_openai, house_gen
from sim.gateways import Gateways
from sim.life import Life
from sim.server import ServerThread
from sim.sim_config import SimConfig

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--panel-port", type=int, default=8090)
    ap.add_argument(
        "--house", choices=sorted(house_gen.PRESETS), help="house size (default: last used, else realistic)"
    )
    ap.add_argument(
        "--backfill-days", type=int, help="days of history to write at start (default: last used, else 14)"
    )
    ap.add_argument("--quiet-house", action="store_true", help="start with the event flow paused")
    args = ap.parse_args()

    data_dir = Path(os.environ.get("AH_DATA_DIR", ".data"))
    cfg = SimConfig.load(data_dir / "sim")
    if args.house:
        cfg.house = {"preset": args.house}
    if args.backfill_days is not None:
        cfg.backfill_days = max(0, min(args.backfill_days, 30))

    env = {
        "AH_DATA_DIR": str(data_dir),
        "AH_HA_URL": "http://127.0.0.1:8123",
        "AH_HA_TOKEN": "sim-token",
        "AH_ALLOWED_PEERS": "127.0.0.1",
        "AH_LLM_URL": "http://127.0.0.1:8080",
        "AH_JETSON_METRICS_URL": "http://127.0.0.1:9100",
        "AH_JETSON_TOKEN": "jetson-sim",
        "AH_ANTHROPIC_BASE_URL": "http://127.0.0.1:8081",
        "AH_ANTHROPIC_API_KEY": "sk-ant-sim",
        "AH_OPENAI_BASE_URL": "http://127.0.0.1:8082/v1",
        "AH_OPENAI_API_KEY": "sk-proj-sim",
        "AH_SHARE_DIR": str(ROOT / "owner-reports"),  # ./nodered: the owner's flow exports
    }
    for k, v in env.items():
        os.environ.setdefault(k, v)
    sys.path.insert(0, str(ROOT / "agent_house"))

    ha = fake_ha.HAState.default()
    llm = fake_llm.LLMSim()
    jetson = fake_jetson.JetsonSim(llm)
    fakes = {  # the plain fakes, on free ports; the gateways in front of them take the known ports
        "llm": ServerThread(fake_llm.create_app(llm)),
        "anthropic": ServerThread(fake_anthropic.create_app()),
        "openai": ServerThread(fake_openai.create_app()),
        "jetson": ServerThread(fake_jetson.create_app(jetson)),
    }
    for s in fakes.values():
        s.start()
    gw = Gateways(cfg, jetson, {k: s.url for k, s in fakes.items()})
    sim = control.Sim(ha, llm, jetson, cfg, gw, app_url=f"http://127.0.0.1:{args.port + 1}/")
    servers = [
        ServerThread(fake_ha.create_app(ha), port=8123),
        ServerThread(gw.llm_app(), port=8080),
        ServerThread(gw.cloud_app("anthropic"), port=8081),
        ServerThread(gw.cloud_app("openai"), port=8082),
        ServerThread(gw.metrics_app(), port=9100),
        ServerThread(fake_ingress.create_app(f"http://127.0.0.1:{args.port}"), port=args.port + 1),
        ServerThread(control.create_app(sim, args.panel_port), port=args.panel_port),
    ]
    for s in servers:
        s.start()

    async def grow() -> dict:  # on the fake HA's loop, before the add-on connects
        sim.life = Life(ha)
        sim.life.rate = 0.0 if args.quiet_house else 1.0
        result = sim.rebuild(sim.spec(), cfg.backfill_days)
        sim.life.start()
        return result

    assert ha.loop is not None
    house = asyncio.run_coroutine_threadsafe(grow(), ha.loop).result(timeout=60)
    cfg.save()

    from app.main import create_app  # noqa: PLC0415  (env must be set first)

    info = house["summary"]
    print(
        f"\n  Agent House sim ready: {info['entities']} entities, {info['devices']} devices, "
        f"{house['backfill'].get('days', 0)} days of history."
        f"\n    add-on:    http://127.0.0.1:{args.port + 1}/"
        f"\n    sim panel: http://127.0.0.1:{args.panel_port}/"
        f"\n    room LLM: {cfg.room.backend}, Claude: {cfg.anthropic.backend}, OpenAI: {cfg.openai.backend}, "
        f"Jetson metrics: {cfg.metrics_source()}\n",
        flush=True,
    )
    try:
        # the GUI keeps an EventSource open: without a timeout, Ctrl+C waits for the browser forever
        uvicorn.run(
            create_app(), host="127.0.0.1", port=args.port, log_level="info", timeout_graceful_shutdown=3
        )
    finally:
        for s in [*servers, *fakes.values()]:
            s.stop()


if __name__ == "__main__":
    main()
