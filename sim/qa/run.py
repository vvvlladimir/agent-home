"""AI testers against the polygon (docs/POLYGON.md "AI testers").

    python -m sim.qa run --roles newcomer migrator fussy_tenant -- --orchestrator openai \
        --room-llm compatible --room-model Qwen/Qwen3.5-4B --no-pace --backfill-days 14
    python -m sim.qa coordinate .agent-logs/qa/<stamp>
    python -m sim.qa verify .agent-logs/qa/<stamp>
    python -m sim.qa claude                     # all of it on Claude Code alone, no keys (D30)

`run`: one `sim.polygon serve` per role (own ports, own data dir) and one headless Claude Code
session per role (`claude -p`, cheap model, only `curl` and file tools), in parallel. Each writes
`<out>/<role>/findings.json`.
`coordinate`: validates the findings, groups likely duplicates deterministically (same endpoint and
kind, similar text), asks the coordinator model (Qwen through an OpenAI-compatible API) one yes/no
question per candidate pair, and writes `backlog.json` + `backlog.md`.
`verify`: replays each backlog item's steps on a fresh polygon with a fresh tester that sees only
the steps; only items reproduced there are `confirmed`.
`claude`: run + coordinate + verify where every model is a `claude -p` session (room agents haiku/low,
orchestrator sonnet/medium, testers haiku, attackers sonnet, coordinator haiku/low, verifier
sonnet/medium); needs only a logged-in `claude` CLI, e.g. a claude.ai/code cloud session.
`up`, `pairs`, `coordinate --same`, `verify-up`, `verify-collect`, `down`: the same loop driven by a
lead Claude Code session whose subagents are the testers and verifiers (skill `/polygon-claude`).
Everything model-related is outside the add-on; nothing here runs in `make test` except the pure
helpers (`validate`, `group`, `render`).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
KINDS = ("bug", "missing_setting", "unclear", "safety", "ux")
SEVERITIES = ("critical", "major", "minor")
REQUIRED = ("title", "kind", "severity", "steps", "expected", "actual", "endpoint")
SIMILAR = 0.35  # token Jaccard above which two findings are a candidate pair for the coordinator
COORDINATOR_URL = "https://api.deepinfra.com/v1/openai"
COORDINATOR_MODEL = "Qwen/Qwen3.5-4B"


# --- pure helpers (tested) ---------------------------------------------------------------------
def validate(item: dict) -> list[str]:
    """Problems with one finding; [] = usable."""
    if not isinstance(item, dict):
        return ["not an object"]
    problems = [f"missing {k}" for k in REQUIRED if not item.get(k)]
    if item.get("kind") not in KINDS:
        problems.append(f"kind not in {KINDS}")
    if item.get("severity") not in SEVERITIES:
        problems.append(f"severity not in {SEVERITIES}")
    if not isinstance(item.get("steps"), list):
        problems.append("steps is not a list")
    if item.get("reproduced_by_me") is not True:
        problems.append("not reproduced by the tester")
    return problems


def _tokens(item: dict) -> set[str]:
    text = f"{item.get('title', '')} {item.get('actual', '')}".lower()
    return {w for w in re.findall(r"[a-z_][a-z0-9_.]{2,}", text)}


def _endpoint(item: dict) -> str:
    return re.sub(r"/[a-z]_[A-Za-z0-9]+|/\d+", "/{id}", str(item.get("endpoint") or "").split("?")[0])


def candidate_pairs(items: list[dict]) -> list[tuple[int, int]]:
    """Pairs worth asking the coordinator about: same endpoint and kind, similar words."""
    pairs = []
    for i, a in enumerate(items):
        for j in range(i + 1, len(items)):
            b = items[j]
            if _endpoint(a) != _endpoint(b) or a.get("kind") != b.get("kind"):
                continue
            ta, tb = _tokens(a), _tokens(b)
            if ta and tb and len(ta & tb) / len(ta | tb) >= SIMILAR:
                pairs.append((i, j))
    return pairs


def group(items: list[dict], same: set[tuple[int, int]]) -> list[dict]:
    """Union-find over the pairs judged the same; one backlog entry per group, worst severity wins."""
    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in same:
        parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(i)
    out = []
    for n, members in enumerate(sorted(groups.values(), key=lambda m: m[0]), 1):
        lead = min(members, key=lambda i: SEVERITIES.index(items[i]["severity"]))
        roles = sorted({items[i].get("role", "?") for i in members})
        out.append({"id": f"QA-{n:03d}", **items[lead], "reported_by": roles, "duplicates": len(members) - 1,
                    "status": "candidate"})  # fmt: skip
    return sorted(out, key=lambda e: (SEVERITIES.index(e["severity"]), -len(e["reported_by"])))


def render(backlog: list[dict]) -> str:
    lines = [
        "# QA backlog",
        "",
        "| id | severity | kind | status | title | roles |",
        "|---|---|---|---|---|---|",
    ]
    for e in backlog:
        lines.append(f"| {e['id']} | {e['severity']} | {e['kind']} | {e['status']} | {e['title']} | "
                     f"{', '.join(e['reported_by'])} |")  # fmt: skip
    for e in backlog:
        lines += ["", f"## {e['id']} {e['title']}", f"- expected: {e['expected']}",
                  f"- actual: {e['actual']}",
                  f"- endpoint: `{e['endpoint']}` · run: `{e.get('run_id')}`", "- steps:"]  # fmt: skip
        lines += [f"  1. `{s}`" for s in e["steps"]]
        if e.get("verify"):
            lines.append(f"- verify: {e['verify']}")
    return "\n".join(lines) + "\n"


# --- processes -----------------------------------------------------------------------------------
def start_polygon(
    out: Path, polygon_args: list[str], detached: bool = False, wait_s: float = 300.0
) -> tuple[subprocess.Popen, dict]:
    """`sim.polygon serve` in the background; its first stdout line is the meta JSON. Detached
    polygons outlive this process (own session) and are stopped with `python -m sim.qa down`."""
    out.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "sim.polygon", "serve", "--out-dir", str(out), *polygon_args]
    meta_file = out / "polygon.out"
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=meta_file.open("w"), stderr=(out / "polygon.log").open("w"),
                            text=True, start_new_session=detached)  # fmt: skip
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline and proc.poll() is None:
        line = meta_file.read_text().split("\n", 1)[0].strip()
        if line.startswith("{") and meta_file.read_text().count("\n"):
            return proc, json.loads(line)
        time.sleep(0.5)
    proc.terminate()
    raise SystemExit(f"polygon did not start, see {out / 'polygon.log'}")


def _remember(out: Path, proc: subprocess.Popen) -> None:
    f = out / "pids.json"
    pids = json.loads(f.read_text()) if f.exists() else []
    f.write_text(json.dumps([*pids, proc.pid]))


def model_args(models: str) -> tuple[str, list[str]]:
    """Backends for the models inside the add-on: `claude -p` when it works here, else the fakes."""
    if models in ("auto", "claude"):
        from sim import claude_bridge  # noqa: PLC0415

        try:
            if claude_bridge.ask("Is 2 + 2 equal to 4?").get("same") is True:
                return "claude", CLAUDE_POLYGON
        except claude_bridge.BridgeError as err:
            if models == "claude":
                raise SystemExit(f"claude -p does not work here: {err}") from None
    return "fake", ["--backfill-days", "14"]


CLAUDE_POLYGON = ["--orchestrator", "claude", "--room-llm", "claude", "--no-pace", "--backfill-days", "14"]
CLAUDE_ROLE_MODELS = {"intruder": "sonnet", "chaos": "sonnet"}  # attackers need the stronger model


def tester(prompt: str, cwd: Path, model: str, max_turns: int, effort: str | None = None) -> subprocess.Popen:
    if shutil.which("claude") is None:
        raise SystemExit("the `claude` CLI is not on PATH")
    cmd = ["claude", "-p", prompt, "--model", model, "--max-turns", str(max_turns), "--output-format", "json",
           "--allowedTools", "Bash(curl:*)", "Read", "Write"]  # fmt: skip
    if effort:
        cmd += ["--effort", effort]
    return subprocess.Popen(cmd, cwd=cwd, stdout=(cwd / "session.json").open("w"), stderr=subprocess.STDOUT)


def brief(role: str, spec: dict, meta: dict, out: Path, max_calls: int) -> str:
    return (HERE / "brief.md").read_text().format(
        role=role, traits=json.dumps(spec["traits"]), goal=spec["goal"].strip(),
        focus=", ".join(spec["focus"]),
        app=meta["urls"]["app"].rstrip("/"), panel=meta["urls"]["panel"].rstrip("/"),
        openapi=ROOT / "contracts" / "openapi.yaml", out=out, max_calls=max_calls,
    )  # fmt: skip


def cmd_run(args: argparse.Namespace) -> int:
    roles = yaml.safe_load((HERE / "roles.yaml").read_text())["roles"]
    chosen = list(roles) if args.roles == ["all"] else args.roles
    unknown = sorted(set(chosen) - set(roles))
    if unknown:
        raise SystemExit(f"unknown roles: {unknown}")
    out = args.out or ROOT / ".agent-logs" / "qa" / datetime.now().strftime("%Y%m%d-%H%M%S")
    polygons, sessions = [], []
    try:
        for role in chosen:
            work = out / role
            proc, meta = start_polygon(work / "polygon", args.polygon)
            polygons.append(proc)
            (work / "polygon.json").write_text(json.dumps(meta, indent=2, default=str))
            model = getattr(args, "role_models", {}).get(role, args.tester_model)
            sessions.append((role, tester(brief(role, roles[role], meta, work, args.max_calls), work,
                                          model, args.max_turns, args.tester_effort)))  # fmt: skip
            print(f"{role}: {meta['urls']['app']}", flush=True)
        deadline = time.monotonic() + args.timeout
        for role, session in sessions:
            try:
                session.wait(max(1.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                session.terminate()
                print(f"{role}: timed out", flush=True)
    finally:
        for proc in polygons:
            proc.terminate()
    args.dir = out
    print(json.dumps({"out": str(out)}))
    return 0


# --- coordinator -----------------------------------------------------------------------------------
def ask_same(a: dict, b: dict, url: str, model: str, key: str) -> bool:
    """One yes/no question to the coordinator model: same root cause?"""
    import httpx

    def short(x: dict) -> str:
        return json.dumps(
            {k: x.get(k) for k in ("title", "endpoint", "expected", "actual")}, ensure_ascii=False
        )

    prompt = ("Two bug reports from testers of one app. Do they describe the same faulty behaviour under "
              "materially the same conditions? Answer only JSON {\"same\": true|false, \"why\": \"...\"}.\n"
              f"A: {short(a)}\nB: {short(b)}")  # fmt: skip
    response = httpx.post(
        f"{url.rstrip('/')}/chat/completions", headers={"Authorization": f"Bearer {key}"}, timeout=60,
        json={"model": model, "temperature": 0, "max_tokens": 120,
              "messages": [{"role": "user", "content": prompt}]},
    )  # fmt: skip
    response.raise_for_status()
    text = response.json()["choices"][0]["message"]["content"] or ""
    found = re.search(r"\{.*\}", text, re.S)
    try:
        return bool(found and json.loads(found.group(0)).get("same") is True)
    except json.JSONDecodeError:
        return False  # unsure = keep both: a duplicate costs less than a lost finding


def ask_same_claude(a: dict, b: dict) -> bool:
    """The same question through `claude -p` (haiku, low effort) instead of an API key."""
    from sim import claude_bridge  # noqa: PLC0415

    def short(x: dict) -> str:
        keys = ("title", "endpoint", "expected", "actual")
        return json.dumps({k: x.get(k) for k in keys}, ensure_ascii=False)

    question = ("Two bug reports from testers of one app. Do they describe the same faulty behaviour under "
                f"materially the same conditions?\nA: {short(a)}\nB: {short(b)}")  # fmt: skip
    try:
        return claude_bridge.ask(question).get("same") is True
    except claude_bridge.BridgeError:
        return False  # unsure = keep both


def load_findings(directory: Path) -> tuple[list[dict], list[dict]]:
    items, rejected = [], []
    for path in sorted(directory.glob("*/findings.json")):
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as err:
            rejected.append({"file": str(path), "problems": [f"not JSON: {err}"]})
            continue
        for item in raw if isinstance(raw, list) else []:
            problems = validate(item)
            if problems:
                rejected.append({"file": str(path), "title": (item or {}).get("title"), "problems": problems})
            else:
                items.append({**item, "role": path.parent.name})
    return items, rejected


def cmd_coordinate(args: argparse.Namespace) -> int:
    items, rejected = load_findings(args.dir)
    key = os.environ.get("AH_SIM_COMPATIBLE_API_KEY", "")
    same = set()
    judged = {tuple(p) for p in json.loads(args.same)} if getattr(args, "same", None) else None
    for i, j in candidate_pairs(items):
        if judged is not None:  # the lead session judged the pairs itself
            if (i, j) in judged:
                same.add((i, j))
        elif getattr(args, "claude", False) and not args.no_model:
            if ask_same_claude(items[i], items[j]):
                same.add((i, j))
        elif key and not args.no_model:
            if ask_same(items[i], items[j], args.url, args.model, key):
                same.add((i, j))
        elif items[i]["title"].lower() == items[j]["title"].lower():
            same.add((i, j))  # without the model only identical titles merge
    backlog = group(items, same)
    (args.dir / "backlog.json").write_text(json.dumps({"backlog": backlog, "rejected": rejected}, indent=2))
    (args.dir / "backlog.md").write_text(render(backlog))
    print(json.dumps({"findings": len(items), "backlog": len(backlog), "rejected": len(rejected)}))
    return 0


def verify_prompt(entry: dict, meta: dict, work: Path) -> str:
    return (
        f"Agent House add-on API: {meta['urls']['app']} ; house panel: {meta['urls']['panel']} "
        "(writes need header X-Sim-Panel: 1). Replay these steps with curl, adapting ids and "
        f"ports to this instance: {json.dumps(entry['steps'])}. Expected: {entry['expected']}. "
        f"Reported: {entry['actual']}. Write {work}/verdict.json as "
        '{"reproduced": true|false, "evidence": "quoted response"} and nothing else.'
    )


def apply_verdicts(directory: Path) -> dict:
    """Read `verify/<id>/verdict.json` into the backlog; no verdict keeps the item a candidate."""
    data = json.loads((directory / "backlog.json").read_text())
    for entry in data["backlog"]:
        if entry["status"] != "candidate":
            continue
        try:
            verdict = json.loads((directory / "verify" / entry["id"] / "verdict.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        entry["status"] = {True: "confirmed", False: "not_reproduced"}.get(
            verdict.get("reproduced"), "candidate"
        )
        entry["verify"] = str(verdict.get("evidence"))[:500]
    (directory / "backlog.json").write_text(json.dumps(data, indent=2))
    (directory / "backlog.md").write_text(render(data["backlog"]))
    return {
        s: sum(e["status"] == s for e in data["backlog"])
        for s in ("confirmed", "not_reproduced", "candidate")
    }


def cmd_verify(args: argparse.Namespace) -> int:
    data = json.loads((args.dir / "backlog.json").read_text())
    for entry in data["backlog"]:
        if entry["status"] != "candidate":
            continue
        work = args.dir / "verify" / entry["id"]
        proc, meta = start_polygon(work / "polygon", args.polygon)
        session = None
        try:
            session = tester(
                verify_prompt(entry, meta, work), work, args.tester_model, 30, args.tester_effort
            )
            session.wait(args.timeout)
        except subprocess.TimeoutExpired:
            if session:
                session.terminate()
        finally:
            proc.terminate()
    print(json.dumps(apply_verdicts(args.dir)))
    return 0


# --- the lead session drives it: polygons here, testers and verifiers as its subagents (D30) --------
def cmd_up(args: argparse.Namespace) -> int:
    """Start one detached polygon per role and write each role's brief; print where they are."""
    roles = yaml.safe_load((HERE / "roles.yaml").read_text())["roles"]
    chosen = list(roles) if args.roles == ["all"] else args.roles
    if unknown := sorted(set(chosen) - set(roles)):
        raise SystemExit(f"unknown roles: {unknown}")
    out = args.out or ROOT / ".agent-logs" / "qa" / datetime.now().strftime("%Y%m%d-%H%M%S")
    backend, extra = model_args(args.models)
    listing = {}
    for role in chosen:
        work = out / role
        proc, meta = start_polygon(work / "polygon", [*extra, *args.polygon], detached=True)
        _remember(out, proc)
        (work / "polygon.json").write_text(json.dumps(meta, indent=2, default=str))
        (work / "brief.md").write_text(brief(role, roles[role], meta, work, args.max_calls))
        listing[role] = {"brief": str(work / "brief.md"), "findings": str(work / "findings.json"),
                         "app": meta["urls"]["app"]}  # fmt: skip
    print(json.dumps({"out": str(out), "models": backend, "roles": listing}, indent=2))
    return 0


def cmd_pairs(args: argparse.Namespace) -> int:
    """Candidate duplicate pairs for the lead to judge (indexes as `coordinate --same` takes them)."""
    items, _ = load_findings(args.dir)
    keys = ("role", "title", "endpoint", "expected", "actual")
    pick = lambda n: {k: items[n].get(k) for k in keys}  # noqa: E731
    pairs = [{"pair": [i, j], "a": pick(i), "b": pick(j)} for i, j in candidate_pairs(items)]
    print(json.dumps(pairs, indent=2, ensure_ascii=False))
    return 0


def cmd_verify_up(args: argparse.Namespace) -> int:
    """A fresh detached polygon and a replay prompt per backlog candidate."""
    data = json.loads((args.dir / "backlog.json").read_text())
    _, extra = model_args(args.models)
    listing = {}
    for entry in data["backlog"]:
        if entry["status"] != "candidate":
            continue
        work = args.dir / "verify" / entry["id"]
        proc, meta = start_polygon(work / "polygon", [*extra, *args.polygon], detached=True)
        _remember(args.dir, proc)
        (work / "prompt.md").write_text(verify_prompt(entry, meta, work))
        listing[entry["id"]] = {"prompt": str(work / "prompt.md"), "severity": entry["severity"]}
    print(json.dumps(listing, indent=2))
    return 0


def cmd_verify_collect(args: argparse.Namespace) -> int:
    print(json.dumps(apply_verdicts(args.dir)))
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    import signal  # noqa: PLC0415

    f = args.dir / "pids.json"
    pids = json.loads(f.read_text()) if f.exists() else []
    stopped = 0
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGTERM)
            stopped += 1
        except (ProcessLookupError, PermissionError):
            pass
    f.unlink(missing_ok=True)
    print(json.dumps({"stopped": stopped}))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    polygon_args = argv[argv.index("--") + 1 :] if "--" in argv else []
    argv = argv[: argv.index("--")] if "--" in argv else argv
    ap = argparse.ArgumentParser(prog="python -m sim.qa", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="testers on their own polygons; args after -- go to sim.polygon serve")
    run.add_argument("--roles", nargs="+", default=["newcomer", "migrator", "fussy_tenant"], help="or `all`")
    run.add_argument("--tester-model", default="haiku", help="claude --model for the testers")
    run.add_argument("--max-turns", type=int, default=60)
    run.add_argument("--max-calls", type=int, default=80)
    run.add_argument("--timeout", type=float, default=3600.0, help="seconds for all testers")
    run.add_argument("--out", type=Path)
    coord = sub.add_parser("coordinate", help="validate, deduplicate, write the backlog")
    coord.add_argument("dir", type=Path)
    coord.add_argument("--url", default=COORDINATOR_URL)
    coord.add_argument("--model", default=COORDINATOR_MODEL)
    coord.add_argument("--no-model", action="store_true", help="merge identical titles only")
    verify = sub.add_parser("verify", help="replay each candidate on a fresh polygon")
    verify.add_argument("dir", type=Path)
    verify.add_argument("--tester-model", default="haiku")
    verify.add_argument("--timeout", type=float, default=900.0)
    coord.add_argument("--claude", action="store_true", help="ask `claude -p` (haiku) instead of an API")
    coord.add_argument("--same", help='pairs judged the same, JSON, e.g. "[[0, 2]]" (from `pairs`)')
    up = sub.add_parser("up", help="detached polygons + briefs per role, for testers run as subagents")
    up.add_argument("--roles", nargs="+", default=["newcomer", "migrator", "fussy_tenant"], help="or `all`")
    up.add_argument("--models", choices=("auto", "claude", "fake"), default="auto",
                    help="models in the add-on: claude -p if it works here (auto), else fakes")  # fmt: skip
    up.add_argument("--max-calls", type=int, default=80)
    up.add_argument("--out", type=Path)
    pairs = sub.add_parser("pairs", help="candidate duplicate pairs to judge")
    pairs.add_argument("dir", type=Path)
    vup = sub.add_parser("verify-up", help="a fresh detached polygon + replay prompt per candidate")
    vup.add_argument("dir", type=Path)
    vup.add_argument("--models", choices=("auto", "claude", "fake"), default="auto")
    vcol = sub.add_parser("verify-collect", help="read the verdicts into the backlog")
    vcol.add_argument("dir", type=Path)
    down = sub.add_parser("down", help="stop the detached polygons of a run")
    down.add_argument("dir", type=Path)
    run.add_argument("--tester-effort", help="claude --effort for the testers")
    verify.add_argument("--tester-effort", help="claude --effort for the verifier")
    allc = sub.add_parser("claude", help="run + coordinate + verify on Claude Code alone (no API keys)")
    allc.add_argument("--roles", nargs="+", default=["all"], help="default: all six")
    allc.add_argument("--tester-model", default="haiku")
    allc.add_argument("--verifier-model", default="sonnet")
    allc.add_argument("--max-turns", type=int, default=60)
    allc.add_argument("--max-calls", type=int, default=80)
    allc.add_argument("--timeout", type=float, default=3600.0, help="seconds for all testers")
    allc.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    args.polygon = polygon_args
    if args.cmd == "claude":
        return cmd_claude(args)
    commands = {"run": cmd_run, "coordinate": cmd_coordinate, "verify": cmd_verify, "up": cmd_up,
                "pairs": cmd_pairs, "verify-up": cmd_verify_up, "verify-collect": cmd_verify_collect,
                "down": cmd_down}  # fmt: skip
    return commands[args.cmd](args)


def cmd_claude(args: argparse.Namespace) -> int:
    from sim import claude_bridge  # noqa: PLC0415

    if not claude_bridge.available():
        raise SystemExit("the `claude` CLI is not on PATH")
    args.polygon = [*CLAUDE_POLYGON, *args.polygon]  # later flags win in argparse
    args.role_models, args.tester_effort = CLAUDE_ROLE_MODELS, None
    cmd_run(args)
    args.claude, args.no_model = True, False
    cmd_coordinate(args)
    args.tester_model, args.tester_effort = args.verifier_model, "medium"
    args.timeout = 900.0
    cmd_verify(args)
    print(json.dumps({"backlog": str(args.dir / "backlog.md")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
