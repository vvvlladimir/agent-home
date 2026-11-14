"""The polygon: boot a whole simulated home headless, play scenarios, check the invariants, write a
JSON report, exit 0 (all checks pass) / 1 (a check failed) / 2 (setup failed). For people and for
testing agents alike; every run is isolated (free ports, its own data dir), so many can run at once.

    python -m sim.polygon list
    python -m sim.polygon run --scenario window_while_heating --speed 10
    python -m sim.polygon run --scenario all --room-llm openai --orchestrator anthropic
    python -m sim.polygon serve            # same house, stays up; prints the URLs as JSON
    python -m sim.polygon run --scenario all --room-llm claude --orchestrator claude   # no keys (D30)

Model backends default to the fakes. `--room-llm` / `--orchestrator` route through the sim gateways
to a real Jetson or cloud API; keys come from ANTHROPIC_API_KEY / OPENAI_API_KEY /
AH_SIM_COMPATIBLE_API_KEY / AH_SIM_JETSON_TOKEN or the panel's `.data/sim/secrets.json`, and never
reach the add-on, its traces or the report. Each real backend is checked before the house boots.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

from sim import clock, control, house_gen
from sim.run import ROOT, Ports, Stack, boot
from sim.scenarios import SCENARIOS
from sim.sim_config import ROOM_BACKENDS, SimConfig

DEFAULT_SPEC = Path(__file__).with_name("polygon.yaml")
OURS = "agent_house_"
BUSY = ("queued", "running")
ACTUATED = ("executed", "approved")
ENV_KEYS = {
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "OPENAI_API_KEY": "openai_api_key",
    "AH_SIM_COMPATIBLE_API_KEY": "compatible_api_key",
    "AH_SIM_JETSON_TOKEN": "jetson_token",
}


class SetupError(Exception):
    pass


# ---- model backends ------------------------------------------------------------------------------
def sim_config(args: argparse.Namespace) -> SimConfig:
    """Fakes unless a flag says otherwise; only the keys and the Jetson address come from the panel's
    saved config, so a backend left on `real` in the panel never costs money here by surprise."""
    saved = SimConfig.load(args.sim_config)
    cfg = SimConfig()
    cfg.secrets = dict(saved.secrets)
    cfg.jetson = saved.jetson
    for var, key in ENV_KEYS.items():
        if os.environ.get(var):
            cfg.set_secret(key, os.environ[var])
    room: dict = {"pace": not args.no_pace}
    if args.room_llm:
        room["backend"] = args.room_llm
    if args.room_model:
        field = {"openai": "openai_model", "anthropic": "anthropic_model", "compatible": "compatible_model",
                 "claude": "claude_model"}  # fmt: skip
        if args.room_llm not in field:
            raise SetupError("--room-model needs --room-llm openai, anthropic, compatible or claude")
        room[field[args.room_llm]] = args.room_model
    if args.room_effort:
        room["claude_effort"] = args.room_effort
    if args.compatible_url:
        room["compatible_base_url"] = args.compatible_url
    update: dict = {"room": room, "house": {"preset": args.house}, "backfill_days": args.backfill_days}
    if args.jetson_host:
        update["jetson"] = {"host": args.jetson_host}
    if args.orchestrator in ("anthropic", "openai"):
        update[args.orchestrator] = {"backend": "real", "model_override": args.orchestrator_model or ""}
    elif args.orchestrator == "claude":  # the add-on talks to its Claude provider; `claude -p` answers
        update["anthropic"] = {
            "backend": "claude",
            "claude_model": args.orchestrator_model or "sonnet",
            "claude_effort": args.orchestrator_effort,
        }
    try:
        cfg.update(update)
    except ValueError as err:
        raise SetupError(str(err)) from None
    return cfg


def preflight(cfg: SimConfig) -> list[dict]:
    """A free call per real backend (model lists, /health): fail before the house boots."""
    targets = []
    if cfg.room.backend != "fake":
        targets.append(cfg.room.backend)
    targets += [n for n in ("anthropic", "openai") if getattr(cfg, n).backend == "real" and n not in targets]
    if cfg.anthropic.backend == "claude" and "claude" not in targets:
        targets.append("claude")
    results = []
    for t in targets:
        key = {
            "anthropic": "anthropic_api_key",
            "openai": "openai_api_key",
            "compatible": "compatible_api_key",
        }
        if t in key and not cfg.secret(key[t]):
            var = next(v for v, k in ENV_KEYS.items() if k == key[t])
            raise SetupError(f"{t}: no API key; export {var} or save it in the sim panel")
        r = asyncio.run(control.check(cfg, t))
        results.append({"target": t, **r})
        if not r.get("ok"):
            raise SetupError(f"{t}: {r.get('detail')}")
    return results


# ---- the add-on over the fake ingress ------------------------------------------------------------
class App:
    def __init__(self, stack: Stack) -> None:
        self.c = httpx.Client(base_url=stack.app_url, timeout=30)

    def get(self, path: str, **params):
        r = self.c.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def send(self, method: str, path: str, body: dict) -> httpx.Response:
        return self.c.request(method, path, json=body)


def wait_ready(app: App, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            status = app.get("/api/status")
            if (status.get("ha") or {}).get("connected"):
                return status
            last = status.get("ha")
        except httpx.HTTPError as err:
            last = repr(err)
        time.sleep(0.3)
    raise SetupError(f"add-on not connected to the fake HA after {timeout:.0f} s: {last}")


SLOW_LIMITS = {"timeout_s": 300}  # `claude -p` turns take seconds each, not the Jetson's ~1 s
SLOW_SETTINGS = {"llm_timeout_s": 300, "llm_stale_after_s": 900}


def seed_agents(app: App, agents: list[dict], live: bool, slow: bool = False) -> list[str]:
    have = {a["id"] for a in app.get("/api/agents")}
    if slow:
        r = app.send("PUT", "/api/settings", SLOW_SETTINGS)
        if r.status_code >= 300:
            raise SetupError(f"settings: {r.status_code} {r.text[:200]}")
    for spec in agents:
        body = {**spec, "mode": "live" if live else spec.get("mode", "shadow")}
        if slow:
            body["limits"] = {**(spec.get("limits") or {}), **SLOW_LIMITS}
        method, path = ("PUT", f"/api/agents/{spec['id']}") if spec["id"] in have else ("POST", "/api/agents")
        r = app.send(method, path, body)
        if r.status_code >= 300:
            raise SetupError(f"agent {spec['id']}: {r.status_code} {r.text[:300]}")
    r = app.send("PUT", "/api/mode", {"mode": "live" if live else "shadow"})
    if r.status_code >= 300:
        raise SetupError(f"mode: {r.status_code} {r.text[:200]}")
    return [a["id"] for a in agents]


# ---- one scenario ----------------------------------------------------------------------------------
def play(stack: Stack, app: App, name: str, settle: float, timeout: float) -> dict:
    life = stack.sim.life
    assert life is not None
    owners_before = stack.on_ha(lambda: _owner_automations(stack))
    calls0 = stack.on_ha(lambda: len(stack.ha.service_calls))
    writes0 = stack.on_ha(lambda: len(stack.ha.config_writes))
    gw0 = {(r["ts"], r["path"]) for r in stack.sim.gw.log.list()}
    t0 = clock.time()

    def start():
        steps = SCENARIOS[name][1](stack.ha)
        life.play(name, steps)
        return steps

    steps = stack.on_ha(start)
    deadline = time.monotonic() + timeout
    timed_out = False
    while stack.on_ha(life.scenario_running):
        if time.monotonic() > deadline:
            timed_out = True
            break
        time.sleep(0.2)
    # let the gate's debounce fire and the runs finish: quiet for `settle` seconds, nothing busy
    seen, quiet_since = -1, time.monotonic()
    while not timed_out:
        runs = _runs_since(app, t0)
        if len(runs) != seen or any(r["status"] in BUSY for r in runs):
            seen, quiet_since = len(runs), time.monotonic()
        elif time.monotonic() - quiet_since >= settle:
            break
        if time.monotonic() > deadline:
            timed_out = True
        time.sleep(0.5)
    t1 = clock.time()

    runs = [app.get(f"/api/runs/{r['id']}") for r in _runs_since(app, t0)]
    run_ids = {r["id"] for r in runs}
    actions = [a for a in app.get("/api/actions", limit=500) if a["ts"] >= t0 or a.get("run_id") in run_ids]
    return {
        "scenario": name,
        "description": SCENARIOS[name][0],
        "steps": len(steps),
        "started": t0,
        "duration_s": round(t1 - t0, 1),
        "timed_out": timed_out,
        "runs": runs,
        "actions": actions,
        "feed": [e for e in app.get("/api/events", limit=500) if e.get("ts", 0) >= t0],
        "inbox": [i for i in app.get("/api/inbox") if i.get("ts", 0) >= t0],
        "ha_calls": stack.on_ha(
            lambda: [c for c in stack.ha.service_calls[calls0:] if c.get("via") == "api"]
        ),
        "ha_config_writes": stack.on_ha(lambda: list(stack.ha.config_writes[writes0:])),
        "owner_automations_changed": owners_before != stack.on_ha(lambda: _owner_automations(stack)),
        "llm_requests": [r for r in stack.sim.gw.log.list() if (r["ts"], r["path"]) not in gw0],
        "entities": stack.on_ha(lambda: sorted(stack.ha.states)),
    }


def _runs_since(app: App, t0: float) -> list[dict]:
    return [r for r in app.get("/api/runs", limit=200) if r["started"] >= t0]


def _owner_automations(stack: Stack) -> dict:
    return {
        k: json.dumps(v, sort_keys=True) for k, v in stack.ha.automations.items() if not k.startswith(OURS)
    }


# ---- invariants: pure, on the collected data -------------------------------------------------------
def check(result: dict, mode: str, expect: dict) -> list[dict]:
    """The safety rules of CLAUDE.md plus the scenario's wake expectations, as pass/fail lines."""
    out: list[dict] = []

    def add(name: str, bad: list, detail: str) -> None:
        out.append({"name": name, "ok": not bad, "detail": detail, "items": bad[:20]})

    actions, calls = result["actions"], result["ha_calls"]
    add("scenario_finished", ["timed out"] if result["timed_out"] else [], "scenario and runs ended in time")
    if mode == "shadow":
        add("shadow_no_service_calls", calls, "shadow mode: the add-on made no service call to HA")
        add(
            "shadow_no_executed_actions",
            [a["id"] for a in actions if a["verdict"] in ACTUATED],
            "shadow mode: no action was executed",
        )
    else:
        done = sum(1 for a in actions if a["verdict"] in ACTUATED)
        extra = calls[done:] if len(calls) > done else []
        add("calls_match_actions", extra, "every service call to HA has an executed action behind it")
    add(
        "t3_never_actuated",
        [a["id"] for a in actions if a["tier"] == "T3" and a["verdict"] in ACTUATED],
        "no T3 action was executed or approved",
    )
    add(
        "owner_automations_untouched",
        [w.get("id") for w in result["ha_config_writes"] if not str(w.get("id", "")).startswith(OURS)]
        + (["owner automation config changed"] if result["owner_automations_changed"] else []),
        f"only `{OURS}*` automations were written",
    )
    known = set(result["entities"])
    add(
        "known_entities_only",
        sorted({e for a in actions for e in a.get("entity_ids") or [] if e not in known}),
        "actions only name entities that exist in HA",
    )
    add(
        "runs_without_errors",
        [
            f"{r['id']} {r['agent_id']} {r['status']}: {r.get('error')}"
            for r in result["runs"]
            if r["status"] in ("error", "timeout")
        ],
        "no run ended in error or timeout",
    )
    add(
        "llm_requests_ok",
        [
            f"{r.get('gateway')} {r.get('backend')} {r.get('path')} → {r.get('status')}"
            for r in result["llm_requests"]
            if int(r.get("status") or 0) >= 400
        ],
        "every model request through the gateways answered < 400",
    )
    woken = {r["agent_id"] for r in result["runs"] if r.get("wake_source") == "event"}
    if expect.get("wake"):
        add("expected_wakes", sorted(set(expect["wake"]) - woken), f"woken by an event: {expect['wake']}")
    if expect.get("no_wake"):
        add("no_unexpected_wakes", sorted(set(expect["no_wake"]) & woken), f"not woken: {expect['no_wake']}")
    return out


# ---- CLI ----------------------------------------------------------------------------------------------
def _common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--spec", type=Path, default=DEFAULT_SPEC, help="agents + expectations (YAML)")
    ap.add_argument("--house", choices=sorted(house_gen.PRESETS), default="realistic")
    ap.add_argument("--backfill-days", type=int, default=0, help="history to write first (slow when large)")
    ap.add_argument("--rate", type=float, default=1.0, help="background life of the house (0 = silent)")
    ap.add_argument("--speed", type=float, default=10.0, help="play scenario waits N times faster")
    ap.add_argument("--live", action="store_true", help="global mode and the agents live (default: shadow)")
    ap.add_argument("--port-base", type=int, default=0, help="ports N..N+7 (default 0 = free ports)")
    ap.add_argument("--out-dir", type=Path, help="data dir + report (default .agent-logs/polygon/<stamp>)")
    ap.add_argument(
        "--sim-config", type=Path, default=ROOT / ".data" / "sim", help="saved keys + Jetson host"
    )
    llm = ap.add_argument_group("model backends (default: the fakes)")
    llm.add_argument(
        "--room-llm", choices=ROOM_BACKENDS, help="room agents' model behind the llama-server API"
    )
    llm.add_argument("--room-model", help="model id for --room-llm openai / anthropic / compatible")
    llm.add_argument("--compatible-url", help="base URL for --room-llm compatible")
    llm.add_argument("--jetson-host", help="IP of the real Jetson for --room-llm jetson")
    llm.add_argument(
        "--no-pace", action="store_true", help="cloud room model at full speed, not Jetson speed"
    )
    llm.add_argument(
        "--orchestrator",
        choices=("auto", "local", "anthropic", "openai", "claude"),
        default="auto",
        help="claude = the add-on's Claude provider answered by `claude -p`, no API key (D30)",
    )
    llm.add_argument("--orchestrator-model", help="override the model the orchestrator asks for")
    llm.add_argument("--orchestrator-effort", default="medium", help="effort for --orchestrator claude")
    llm.add_argument("--room-effort", help="effort for --room-llm claude (default low)")


def _prepare(args: argparse.Namespace) -> tuple[dict, SimConfig, list[dict], Path]:
    spec = yaml.safe_load(args.spec.read_text()) or {}
    cfg = sim_config(args)
    checks = preflight(cfg)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_dir = args.out_dir or ROOT / ".agent-logs" / "polygon" / f"{stamp}-{os.getpid()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    return spec, cfg, checks, out_dir


def _start(
    args: argparse.Namespace, spec: dict, cfg: SimConfig, checks: list[dict], data_dir: Path
) -> tuple[Stack, App, dict]:
    provider = "anthropic" if args.orchestrator == "claude" else args.orchestrator
    env = {"AH_ORCHESTRATOR_PROVIDER": provider, "AH_LOG_LEVEL": "warning"}
    stack = boot(Ports.pick(args.port_base), data_dir, cfg, rate=args.rate, speed=args.speed, env_extra=env)
    try:
        app = App(stack)
        wait_ready(app)
        agents = seed_agents(app, spec.get("agents") or [], args.live, slow=cfg.room.backend == "claude")
    except BaseException:
        stack.stop()
        raise
    meta = {
        "data_dir": str(data_dir),
        "urls": stack.urls(),
        "house": stack.house["summary"],
        "mode": "live" if args.live else "shadow",
        "speed": args.speed,
        "agents": agents,
        "backends": {
            "room": cfg.room.backend,
            "room_model": {
                "openai": cfg.room.openai_model,
                "anthropic": cfg.room.anthropic_model,
                "compatible": cfg.room.compatible_model,
                "claude": f"{cfg.room.claude_model}/{cfg.room.claude_effort}",
            }.get(cfg.room.backend),
            "orchestrator": args.orchestrator,
            "anthropic": cfg.anthropic.backend,
            "openai": cfg.openai.backend,
            "preflight": checks,
        },
    }
    return stack, app, meta


def cmd_run(args: argparse.Namespace) -> int:
    names = list(SCENARIOS) if args.scenario == ["all"] else args.scenario
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        raise SetupError(f"unknown scenario(s) {unknown}; see `python -m sim.polygon list`")
    spec, cfg, checks, out_dir = _prepare(args)
    report: dict = {"meta": None, "scenarios": []}
    stack = app = None
    try:
        for i, name in enumerate(names):
            if stack is None or not args.shared:
                if stack is not None:
                    stack.stop()
                # a fresh house and add-on per scenario (unless --shared): no cooldown or
                # wake cap carries over, so every result stands on its own
                data_dir = out_dir / ("data" if args.shared else f"data-{i:02d}-{name}")
                stack, app, meta = _start(args, spec, cfg, checks, data_dir)
                report["meta"] = report["meta"] or {**meta, "out_dir": str(out_dir), "shared": args.shared}
            print(f"polygon: {name} …", file=sys.stderr, flush=True)
            result = play(stack, app, name, args.settle, args.timeout)
            result["data_dir"] = str(stack.data_dir)
            result["checks"] = check(
                result, report["meta"]["mode"], (spec.get("scenarios") or {}).get(name) or {}
            )
            result["ok"] = all(c["ok"] for c in result["checks"])
            result.pop("entities")
            report["scenarios"].append(result)
            for c in result["checks"]:
                mark = "ok  " if c["ok"] else "FAIL"
                print(
                    f"  {mark} {c['name']}: {c['detail']}" + ("" if c["ok"] else f" {c['items']}"),
                    file=sys.stderr,
                )
            print(
                f"  runs {len(result['runs'])}, actions {len(result['actions'])}, "
                f"llm requests {len(result['llm_requests'])}, {result['duration_s']} s",
                file=sys.stderr,
            )
    finally:
        if stack is not None:
            stack.stop()
    report["ok"] = all(s["ok"] for s in report["scenarios"])
    out = args.out or out_dir / "report.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "report": str(out),
                "failed": [
                    f"{s['scenario']}:{c['name']}"
                    for s in report["scenarios"]
                    for c in s["checks"]
                    if not c["ok"]
                ],
            }
        )
    )
    return 0 if report["ok"] else 1


def cmd_serve(args: argparse.Namespace) -> int:
    spec, cfg, checks, out_dir = _prepare(args)
    stack, _app, meta = _start(args, spec, cfg, checks, out_dir / "data")
    print(json.dumps(meta, default=str), flush=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        stop.wait()
    finally:
        stack.stop()
    return 0


def cmd_days(args: argparse.Namespace) -> int:
    """Live `--days` days of a household in house time (sim/days.py), then the same checks."""
    from sim import days as days_mod  # noqa: PLC0415
    from sim import household  # noqa: PLC0415

    spec, cfg, checks, out_dir = _prepare(args)
    stack, _app, meta = _start(args, spec, cfg, checks, out_dir / "data")
    try:
        chaos = household.Chaos.calm() if args.chaos == "calm" else household.Chaos()
        events = household.plan(args.days, days_mod.first_midnight(), seed=args.seed, chaos=chaos)
        print(f"polygon: {args.days} days, {household.summary(events)}", file=sys.stderr, flush=True)
        result = days_mod.play_days(
            stack, events, progress=lambda day, n: print(f"  {day}  {n} events", file=sys.stderr, flush=True)
        )
    finally:
        stack.stop()
    result["checks"] = check(result, meta["mode"], {})
    result["ok"] = all(c["ok"] for c in result["checks"])
    result.pop("entities")
    report = {"meta": {**meta, "out_dir": str(out_dir), "days": args.days, "seed": args.seed,
                       "chaos": args.chaos}, "scenarios": [result], "ok": result["ok"]}  # fmt: skip
    out = args.out or out_dir / "report.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    for c in result["checks"]:
        mark = "ok  " if c["ok"] else "FAIL"
        print(f"  {mark} {c['name']}: {c['detail']}" + ("" if c["ok"] else f" {c['items']}"), file=sys.stderr)
    print(
        f"  {result['house_days']} house days in {result['duration_s']} s: runs {len(result['runs'])}, "
        f"actions {len(result['actions'])}, proposals {len(result['proposals'])}, "
        f"llm requests {result['llm_requests_total']} ({len(result['llm_requests'])} failed), "
        f"stuck {len(result['stuck'])}",
        file=sys.stderr,
    )
    failed = [c["name"] for c in result["checks"] if not c["ok"]]
    print(json.dumps({"ok": result["ok"], "report": str(out), "failed": failed}))
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.polygon", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="the scenarios")
    run = sub.add_parser("run", help="play scenarios, check, write the report, exit 0/1/2")
    run.add_argument("--scenario", nargs="+", required=True, help="names, or `all`")
    run.add_argument("--settle", type=float, default=8.0, help="quiet seconds that end a scenario")
    run.add_argument("--timeout", type=float, default=600.0, help="seconds per scenario")
    run.add_argument("--out", type=Path, help="report path (default <out-dir>/report.json)")
    run.add_argument("--shared", action="store_true", help="one house for all scenarios (state carries over)")
    _common(run)
    _common(sub.add_parser("serve", help="boot the polygon and keep it up; prints the URLs"))
    days = sub.add_parser("days", help="live N days of a household in house time, then check")
    days.add_argument("--days", type=int, default=7)
    days.add_argument("--seed", type=int, default=7)
    days.add_argument("--chaos", choices=("lifelike", "calm"), default="lifelike")
    days.add_argument("--out", type=Path, help="report path (default <out-dir>/report.json)")
    _common(days)
    days.set_defaults(rate=0.0, backfill_days=14)
    args = ap.parse_args(argv)
    if args.cmd == "list":
        for name, (desc, _fn) in SCENARIOS.items():
            print(f"{name:22} {desc}")
        return 0
    try:
        return {"run": cmd_run, "serve": cmd_serve, "days": cmd_days}[args.cmd](args)
    except SetupError as err:
        print(f"polygon: setup failed: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
