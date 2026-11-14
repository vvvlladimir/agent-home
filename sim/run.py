"""`python -m sim.run`: the real add-on inside a simulated home, steered from the sim panel.

    :8100  the add-on behind a fake ingress (open this)     :8090  the sim panel
    :8123  fake Home Assistant                              :8099  the add-on itself
    :8080  room-agent LLM gateway   :8081 Claude gateway   :8082 OpenAI gateway   :9100 Jetson metrics

`--port-base N` puts all eight on N..N+7 instead (0 = free ports), so several sims can run side by
side; give each its own `--data-dir`. The gateways start on the fakes; the panel switches them to a
real Jetson (by IP) or the real OpenAI / Claude APIs with keys kept in `<data-dir>/sim/secrets.json`.
See docs/RUNBOOK.md "Simulator". `sim/polygon.py` boots the same stack headless.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import uvicorn

from sim import (
    control,
    fake_anthropic,
    fake_ha,
    fake_ingress,
    fake_jetson,
    fake_llm,
    fake_openai,
    house_gen,
    snapshot,
)
from sim.cassette import Cassette
from sim.gateways import Gateways
from sim.life import Life
from sim.server import ServerThread
from sim.sim_config import SimConfig

ROOT = Path(__file__).resolve().parent.parent
INGRESS_TOKEN = "simtoken"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Ports:
    ha: int = 8123
    llm: int = 8080
    anthropic: int = 8081
    openai: int = 8082
    metrics: int = 9100
    addon: int = 8099
    ingress: int = 8100
    panel: int = 8090

    @classmethod
    def pick(cls, base: int | None, addon: int = 8099, panel: int = 8090) -> Ports:
        """None: the classic fixed ports; 0: free ports; N: N..N+7."""
        if base is None:
            return cls(addon=addon, ingress=addon + 1, panel=panel)
        names = list(cls.__dataclass_fields__)
        if base == 0:
            return cls(**{n: free_port() for n in names})
        return cls(**{n: base + i for i, n in enumerate(names)})


@dataclass
class Stack:
    """Everything one simulated home runs; `stop()` shuts it all down."""

    ports: Ports
    data_dir: Path
    cfg: SimConfig
    sim: control.Sim
    ha: fake_ha.HAState
    llm: fake_llm.LLMSim
    house: dict
    env: dict[str, str]
    servers: list[ServerThread] = field(default_factory=list)
    addon: Any = None  # the add-on's FastAPI app when served in-process (app.state.ctx: its context)

    @property
    def app_url(self) -> str:  # the add-on as the browser sees it, through the fake ingress
        return f"http://127.0.0.1:{self.ports.ingress}/api/hassio_ingress/{INGRESS_TOKEN}"

    @property
    def panel_url(self) -> str:
        return f"http://127.0.0.1:{self.ports.panel}"

    def urls(self) -> dict:
        return {"app": f"{self.app_url}/", "panel": f"{self.panel_url}/", "ports": asdict(self.ports)}

    def on_ha(self, fn, timeout: float = 60):
        """Run `fn` on the fake HA's loop from a plain thread and return its result."""

        async def call():
            return fn()

        assert self.ha.loop is not None
        return asyncio.run_coroutine_threadsafe(call(), self.ha.loop).result(timeout=timeout)

    def stop(self) -> None:
        for s in reversed(self.servers):
            s.stop()


def addon_env(ports: Ports, data_dir: Path) -> dict[str, str]:
    """The add-on's environment: every URL points at this stack's own servers."""
    env = {
        "AH_JETSON_TOKEN": "jetson-sim",
        "AH_ANTHROPIC_API_KEY": "sk-ant-sim",
        "AH_OPENAI_API_KEY": "sk-proj-sim",
        "AH_SHARE_DIR": str(ROOT / "owner-reports"),  # ./nodered: the owner's flow exports
    }
    env.update({k: v for k, v in os.environ.items() if k.startswith("AH_")})  # owner overrides
    env.update(
        {
            "AH_DATA_DIR": str(data_dir),
            "AH_PORT": str(ports.addon),
            "AH_HA_URL": f"http://127.0.0.1:{ports.ha}",
            "AH_HA_TOKEN": "sim-token",
            "AH_ALLOWED_PEERS": "127.0.0.1",
            "AH_LLM_URL": f"http://127.0.0.1:{ports.llm}",
            "AH_JETSON_METRICS_URL": f"http://127.0.0.1:{ports.metrics}",
            "AH_ANTHROPIC_BASE_URL": f"http://127.0.0.1:{ports.anthropic}",
            "AH_OPENAI_BASE_URL": f"http://127.0.0.1:{ports.openai}/v1",
        }
    )
    return env


def boot(
    ports: Ports,
    data_dir: Path,
    cfg: SimConfig,
    *,
    rate: float = 1.0,
    speed: float = 1.0,
    serve_addon: bool = True,
    env_extra: dict[str, str] | None = None,
    restore: Path | None = None,
) -> Stack:
    """Start the fakes, the gateways, the panel and (unless `serve_addon` is off) the add-on.
    `restore`: a snapshot folder (sim/snapshot.py) to boot from instead of a fresh house."""
    sys.path.insert(0, str(ROOT / "agent_house"))
    if restore is not None:
        snapshot.prepare(restore, data_dir)
    ha = fake_ha.HAState.default()
    llm = fake_llm.LLMSim()
    jetson = fake_jetson.JetsonSim(llm)
    started: list[ServerThread] = []

    def start(app, port: int = 0) -> ServerThread:
        s = ServerThread(app, port=port).start()
        started.append(s)
        return s

    try:
        fakes = {  # the plain fakes, on free ports; the gateways in front of them take the known ports
            "llm": start(fake_llm.create_app(llm)),
            "anthropic": start(fake_anthropic.create_app()),
            "openai": start(fake_openai.create_app()),
            "jetson": start(fake_jetson.create_app(jetson)),
        }
        gw = Gateways(cfg, jetson, {k: s.url for k, s in fakes.items()})
        sim = control.Sim(ha, llm, jetson, cfg, gw, app_url=f"http://127.0.0.1:{ports.ingress}/")
        sim.cassette = tape = Cassette()  # the models' answers, for snapshots (sim/cassette.py)
        if restore is not None:
            tape.load(restore / "cassette.json")
        start(fake_ha.create_app(ha), ports.ha)
        start(tape.wrap(gw.llm_app()), ports.llm)
        start(tape.wrap(gw.cloud_app("anthropic")), ports.anthropic)
        start(tape.wrap(gw.cloud_app("openai")), ports.openai)
        start(gw.metrics_app(), ports.metrics)
        start(fake_ingress.create_app(f"http://127.0.0.1:{ports.addon}", INGRESS_TOKEN), ports.ingress)
        start(control.create_app(sim, ports.panel), ports.panel)

        async def grow() -> dict:  # on the fake HA's loop, before the add-on connects
            sim.life = Life(ha)
            sim.life.rate, sim.life.speed = rate, speed
            result = sim.rebuild(sim.spec(), 0 if restore is not None else cfg.backfill_days)
            if restore is not None:
                snapshot.restore_ha(ha, restore)
            sim.life.start()
            return result

        assert ha.loop is not None
        house = asyncio.run_coroutine_threadsafe(grow(), ha.loop).result(timeout=60)
        env = {**addon_env(ports, data_dir), **(env_extra or {})}
        stack = Stack(ports, data_dir, cfg, sim, ha, llm, house, env, started)
        sim.take_snapshot = lambda label: snapshot.take(stack, snapshot.next_folder(stack, label), label)
        if serve_addon:
            from app.config import load_settings  # noqa: PLC0415  (needs agent_house on the path)
            from app.main import create_app  # noqa: PLC0415

            stack.addon = create_app(load_settings(env))
            start(stack.addon, ports.addon)
        return stack
    except BaseException:
        for s in reversed(started):
            s.stop()
        raise


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--panel-port", type=int, default=8090)
    ap.add_argument("--port-base", type=int, help="put all eight ports on N..N+7 (0 = free ports)")
    ap.add_argument("--data-dir", type=Path, help="add-on and sim state (default: $AH_DATA_DIR or .data)")
    ap.add_argument(
        "--house", choices=sorted(house_gen.PRESETS), help="house size (default: last used, else realistic)"
    )
    ap.add_argument(
        "--backfill-days", type=int, help="days of history to write at start (default: last used, else 14)"
    )
    ap.add_argument("--quiet-house", action="store_true", help="start with the event flow paused")
    ap.add_argument("--speed", type=float, default=1.0, help="play scenario waits this many times faster")
    args = ap.parse_args()

    data_dir = args.data_dir or Path(os.environ.get("AH_DATA_DIR", ".data"))
    cfg = SimConfig.load(data_dir / "sim")
    if args.house:
        cfg.house = {"preset": args.house}
    if args.backfill_days is not None:
        cfg.backfill_days = max(0, min(args.backfill_days, 30))

    ports = Ports.pick(args.port_base, args.port, args.panel_port)
    stack = boot(
        ports, data_dir, cfg, rate=0.0 if args.quiet_house else 1.0, speed=args.speed, serve_addon=False
    )
    cfg.save()

    from app.config import load_settings  # noqa: PLC0415  (boot put agent_house on the path)
    from app.main import create_app  # noqa: PLC0415

    info = stack.house["summary"]
    print(
        f"\n  Agent House sim ready: {info['entities']} entities, {info['devices']} devices, "
        f"{stack.house['backfill'].get('days', 0)} days of history."
        f"\n    add-on:    http://127.0.0.1:{ports.ingress}/"
        f"\n    sim panel: {stack.panel_url}/"
        f"\n    room LLM: {cfg.room.backend}, Claude: {cfg.anthropic.backend}, OpenAI: {cfg.openai.backend}, "
        f"Jetson metrics: {cfg.metrics_source()}\n",
        flush=True,
    )
    try:
        # the GUI keeps an EventSource open: without a timeout, Ctrl+C waits for the browser forever
        uvicorn.run(
            create_app(load_settings(stack.env)),
            host="127.0.0.1",
            port=ports.addon,
            log_level="info",
            timeout_graceful_shutdown=3,
        )
    finally:
        stack.stop()


if __name__ == "__main__":
    main()
