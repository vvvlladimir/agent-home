#!/usr/bin/env python3
"""Read owner-reports/ and write the measured-results block of docs/DECISIONS.md (`make analyze-report`).

Understands:
- bench/*.json        → contracts/schemas/bench_report.schema.json
- setup/*.json        → contracts/schemas/setup_check.schema.json
- diagnostics/*.zip   → manifest.json per contracts/schemas/diagnostics_manifest.schema.json

It only reports and recommends; changing the default profile stays a human decision recorded in
DECISIONS.md. Reports with source=sim are listed but never used for a recommendation.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "contracts" / "schemas"
DECISIONS = ROOT / "docs" / "DECISIONS.md"
BEGIN, END = "<!-- analyze-report:begin -->", "<!-- analyze-report:end -->"

# Acceptance targets for the default profile (prompt §6).
MAX_WAKE_MS = 8000
MIN_TOOLCALL = 0.95
MIN_FREE_MB = 1024


def _load(path: Path, schema_name: str) -> tuple[dict | None, str | None]:
    try:
        data = json.loads(path.read_text())
        jsonschema.validate(data, json.loads((SCHEMAS / schema_name).read_text()))
        return data, None
    except (ValueError, jsonschema.ValidationError) as err:
        return None, f"{path.name}: {str(err).splitlines()[0]}"


def _fmt(value, unit="", digits=1) -> str:
    return "—" if value is None else f"{value:.{digits}f}{unit}"


def verdict(report: dict) -> tuple[bool, list[str]]:
    """Does this profile meet the acceptance targets? Missing numbers count as not met."""
    problems = []
    speed, tool, res = report["speed"], report["toolcall"], report.get("resources", {})
    wake = speed.get("e2e_wake_ms_warm")
    if wake is None or wake > MAX_WAKE_MS:
        problems.append(f"warm wake {_fmt(wake, ' ms', 0)} (target ≤ {MAX_WAKE_MS})")
    accuracy = tool["passed"] / tool["total"] if tool["total"] else 0.0
    if accuracy < MIN_TOOLCALL:
        problems.append(f"tool-call accuracy {accuracy:.0%} (target ≥ {MIN_TOOLCALL:.0%})")
    free = res.get("ram_free_min_mb")
    if free is None or free < MIN_FREE_MB:
        problems.append(f"minimum free RAM {_fmt(free, ' MB', 0)} (target ≥ {MIN_FREE_MB})")
    if res.get("swap_peak_mb") and res["swap_peak_mb"] > 256:
        problems.append(f"swap in use ({res['swap_peak_mb']:.0f} MB)")
    if res.get("throttled"):
        problems.append("thermal throttling")
    return not problems, problems


def analyze(reports_dir: Path) -> str:
    lines: list[str] = []
    invalid: list[str] = []

    bench = []
    for path in sorted((reports_dir / "bench").glob("*.json")):
        data, error = _load(path, "bench_report.schema.json")
        if error:
            invalid.append(error)
        else:
            bench.append((path, data))
    if bench:
        lines += [
            "### Benchmarks",
            "",
            "| file | source | profile | prefill tok/s | decode tok/s | TTFT warm | wake warm | "
            "tool calls | min free RAM | meets targets |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        candidates = []
        for path, r in bench:
            ok, problems = verdict(r)
            speed, tool, res = r["speed"], r["toolcall"], r.get("resources", {})
            measured = r["source"] != "sim"
            lines.append(
                f"| {path.name} | {r['source']} | {r['profile']['name']} | {_fmt(speed.get('prefill_tps'))} | "
                f"{_fmt(speed.get('decode_tps'))} | {_fmt(speed.get('ttft_ms_warm'), ' ms', 0)} | "
                f"{_fmt(speed.get('e2e_wake_ms_warm'), ' ms', 0)} | {tool['passed']}/{tool['total']} | "
                f"{_fmt(res.get('ram_free_min_mb'), ' MB', 0)} | "
                f"{('yes' if ok else 'no: ' + '; '.join(problems)) if measured else 'n/a (simulator)'} |"
            )
            if ok and measured:
                candidates.append(r)
        lines.append("")
        if candidates:
            # Among profiles that meet every target, prefer tool-call accuracy, then speed.
            best = max(
                candidates,
                key=lambda r: (
                    r["toolcall"]["passed"] / r["toolcall"]["total"],
                    r["speed"].get("decode_tps") or 0,
                ),
            )
            lines.append(
                f"**Recommendation:** `{best['profile']['name']}` meets all targets and is the best of "
                f"{len(candidates)} qualifying profile(s). Record the decision in the Decisions section above."
            )
        elif any(r["source"] != "sim" for _, r in bench):
            lines.append(
                "**Recommendation:** no measured profile meets all targets; keep the provisional default and see the reasons in the table."
            )
        else:
            lines.append("Only simulator reports found: nothing here is a hardware measurement.")
        lines.append("")

    for path in sorted((reports_dir / "setup").glob("*.json")):
        data, error = _load(path, "setup_check.schema.json")
        if error:
            invalid.append(error)
            continue
        lines += [
            f"### Jetson check — {path.name}",
            "",
            f"- module: {data.get('module')} (Orin Nano 8 GB: {data.get('is_orin_nano_8gb')})",
            f"- L4T {data.get('l4t_version')}, JetPack major {data.get('jetpack_major')}, CUDA {data.get('cuda_version')}",
            f"- power mode: {data.get('power_mode')}; RAM available {_fmt(data.get('ram_available_mb'), ' MB', 0)}"
            f" of {_fmt(data.get('ram_total_mb'), ' MB', 0)}; swap {_fmt(data.get('swap_total_mb'), ' MB', 0)}",
        ]
        lines += [
            f"- {c['status'].upper()}: {c['name']} — {c.get('detail', '')}"
            for c in data["checks"]
            if c["status"] != "ok"
        ]
        lines.append("")

    for path in sorted((reports_dir / "diagnostics").glob("*.zip")):
        try:
            manifest = json.loads(zipfile.ZipFile(path).read("manifest.json"))
            jsonschema.validate(
                manifest, json.loads((SCHEMAS / "diagnostics_manifest.schema.json").read_text())
            )
        except (KeyError, ValueError, zipfile.BadZipFile, jsonschema.ValidationError) as err:
            invalid.append(f"{path.name}: {str(err).splitlines()[0]}")
            continue
        ha, ingress = manifest.get("ha", {}), manifest.get("ingress", {})
        lines += [
            f"### Diagnostics — {path.name}",
            "",
            f"- app {manifest['app_version']} on {manifest.get('arch')} / Python {manifest.get('python')}",
            f"- HA connected: {ha.get('connected')} (version {ha.get('version')}, via Supervisor: {ha.get('via_supervisor')}, "
            f"{ha.get('areas')} areas, {ha.get('entities')} entities)",
            f"- ingress path seen: {ingress.get('last_ingress_path')}; peer {ingress.get('last_peer')}; "
            f"rejected peers {ingress.get('rejected_peers')}",
            "",
        ]

    if invalid:
        lines += ["### Files that did not validate", ""] + [f"- {item}" for item in invalid] + [""]
    if not lines:
        lines = [
            "No owner reports yet. See docs/OWNER_CHECKLIST.md for what to run and where to put the output.",
            "",
        ]
    return "\n".join(lines)


def main() -> int:
    reports_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "owner-reports")
    block = analyze(reports_dir)
    text = DECISIONS.read_text()
    if BEGIN not in text or END not in text:
        print(f"{DECISIONS} has no {BEGIN} … {END} block", file=sys.stderr)
        return 1
    head, rest = text.split(BEGIN, 1)
    DECISIONS.write_text(f"{head}{BEGIN}\n{block}\n{END}{rest.split(END, 1)[1]}")
    print(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
