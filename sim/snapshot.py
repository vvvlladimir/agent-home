"""Snapshots of a whole polygon, and booting one back (step 4: verify by restoring, not by replaying).

A snapshot is a folder:
- `data/`: the add-on's data dir (SQLite copied with the backup API, safe while the add-on writes);
- `ha.json`: the fake HA (states, history, logbook, registries, automations, scripts, contexts);
- `cassette.json`: the models' answers so far (sim/cassette.py), replayed first after a restore;
- `snapshot.json`: house time, label, source.

`python -m sim.polygon serve --restore <folder>` boots a fresh polygon in that state: same proposals,
runs, inbox and history, house time where it was, models answering as they did. A tester takes one
right before writing a finding (`POST <panel>/api/snapshot`), and `sim.qa verify-up` restores it, so
LLM-dependent state (a draft, eight undos, a review) no longer has to be rebuilt from curl steps.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from pathlib import Path

from sim import clock

HA_FIELDS = (
    "areas",
    "devices",
    "entity_registry",
    "states",
    "automations",
    "scripts",
    "history",
    "logbook_rows",
    "_origins",
    "generated",
)


def take(stack, dest: Path, label: str = "") -> dict:
    """Write a snapshot of `stack` to `dest` (a new folder) and return its description."""
    dest.mkdir(parents=True, exist_ok=False)
    data = dest / "data"
    shutil.copytree(stack.data_dir, data, ignore=shutil.ignore_patterns("*.db", "*.db-*", "sim"))
    for db in Path(stack.data_dir).glob("*.db"):
        src, dst = sqlite3.connect(db), sqlite3.connect(data / db.name)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()

    def ha_state() -> str:
        ha = stack.ha
        return json.dumps(
            {f: (sorted(getattr(ha, f)) if f == "generated" else getattr(ha, f)) for f in HA_FIELDS}
        )

    (dest / "ha.json").write_text(stack.on_ha(ha_state))
    answers = stack.sim.cassette.save(dest / "cassette.json") if getattr(stack.sim, "cassette", None) else 0
    meta = {
        "label": label,
        "house_time": clock.time(),
        "taken": time.time(),
        "source": str(stack.data_dir),
        "answers": answers,
    }
    (dest / "snapshot.json").write_text(json.dumps(meta, indent=2))
    return {"snapshot": str(dest), **meta}


def next_folder(stack, label: str) -> Path:
    base = Path(stack.data_dir).parent / "snapshots"
    n = len(list(base.glob("*"))) if base.exists() else 0
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "snapshot"
    return base / f"{n:03d}-{slug}"


def prepare(snapshot: Path, data_dir: Path) -> dict:
    """Before boot: the add-on's data into `data_dir`, house time forward to the snapshot's."""
    meta = json.loads((snapshot / "snapshot.json").read_text())
    shutil.copytree(snapshot / "data", data_dir, dirs_exist_ok=True)
    clock.travel_to(meta["house_time"])
    return meta


def restore_ha(ha, snapshot: Path) -> None:
    """On the HA loop, after the house is built: put the fake HA back as it was."""
    saved = json.loads((snapshot / "ha.json").read_text())
    for f in HA_FIELDS:
        if f in saved:
            setattr(ha, f, set(saved[f]) if f == "generated" else saved[f])
