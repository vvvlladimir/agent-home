"""Migration: the owner's existing automations next to what Agent House would replace them with.

- import  — read-only copies of the owner's HA automations and scripts and of Node-RED exports in
            `<share>/nodered/*.json` (`nodered.py`). Ours (`agent_house_*`) are skipped.
- propose — the orchestrator drafts an equivalent automation; it is a normal proposal
            (`replaces_original` set), checked and approved like any other.
- parity  — the original's and the replacement's triggers replayed against the same history
            (`evidence.py`): firings matched within a minute, the rest listed per side.
- enabled — the reversible "disable original": `automation.turn_off` / `turn_on` on the owner's
            automation, only on the owner's explicit request. Nothing of the owner's is ever edited
            or deleted, and scripts or Node-RED flows are not switched from here at all.
            The kill switch blocks disabling; enabling again (the undo) stays possible.
"""

from __future__ import annotations

import json
import logging
import re
import time

import yaml

from ..ha.client import AUTOMATION_PREFIX, HAError
from .actuator import new_id
from .evidence import firings, fully_supported
from .lifecycle import normalize
from .nodered import parse_flows
from .orchestrator import OrchestratorError

log = logging.getLogger(__name__)

MATCH_WINDOW_S = 60.0
MAX_DIFFS = 50
MAX_FLOW_BYTES = 5_000_000
DEAD = ("rejected", "superseded", "rolled_back")
LABEL = {"ha_automation": "Home Assistant automation", "ha_script": "script", "node_red": "Node-RED flow"}


class MigrationError(Exception):
    """`code`: not_found | conflict | kill_switch | ha_error."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:80] or "x"


def _list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _calls(steps) -> list[str]:
    """`light.turn_on light.a, light.b` for every service call in an action tree."""
    out = []
    for step in _list(steps):
        if not isinstance(step, dict):
            continue
        service = step.get("action") or step.get("service")
        if isinstance(service, str):
            target = step.get("target") if isinstance(step.get("target"), dict) else {}
            data = step.get("data") if isinstance(step.get("data"), dict) else {}
            entities = [
                str(e)
                for source in (target.get("entity_id"), data.get("entity_id"), step.get("entity_id"))
                for e in _list(source)
            ]
            out.append(f"{service} {', '.join(entities)}".strip())
        elif "scene" in step:
            out.append(f"scene.turn_on {step['scene']}")
        for key in ("sequence", "then", "else", "default", "parallel"):
            out += _calls(step.get(key))
        for option in _list(step.get("choose")):
            out += _calls((option or {}).get("sequence") if isinstance(option, dict) else None)
        if isinstance(step.get("repeat"), dict):
            out += _calls(step["repeat"].get("sequence"))
    return out


def summarize(config: dict | None) -> str | None:
    """One line: when it fires and what it calls. Deterministic, for the list and the draft request."""
    if not isinstance(config, dict):
        return None
    when = []
    for trigger in _list(config.get("triggers")):
        if not isinstance(trigger, dict):
            continue
        kind = trigger.get("trigger")
        if kind in ("state", "numeric_state"):
            text = ", ".join(str(e) for e in _list(trigger.get("entity_id")))
            if trigger.get("to") is not None:
                text += f" → {trigger['to']}"
            for bound in ("above", "below"):
                if trigger.get(bound) is not None:
                    text += f" {bound} {trigger[bound]}"
            when.append(text)
        elif kind == "time":
            when.append(f"at {', '.join(str(a) for a in _list(trigger.get('at')))}")
        else:
            detail = trigger.get("topic") or trigger.get("event_type") or trigger.get("cron") or ""
            when.append(f"{kind} {detail}".strip())
    calls = list(dict.fromkeys(_calls(config.get("actions") or config.get("sequence"))))
    parts = []
    if when:
        parts.append("When: " + "; ".join(when[:8]) + (f" (+{len(when) - 8})" if len(when) > 8 else ""))
    if calls:
        parts.append("Does: " + "; ".join(calls[:8]) + (f" (+{len(calls) - 8})" if len(calls) > 8 else ""))
    logic = config.get("logic_nodes") or {}
    if logic:
        parts.append(
            "Logic not read: " + ", ".join(f"{count} {kind}" for kind, count in sorted(logic.items()))
        )
    return ". ".join(parts)[:600] or None


def match(original: list[dict], replacement: list[dict]) -> tuple[int, list[dict]]:
    """Pair firings that are at most a minute apart → (matched, the unpaired ones per side)."""
    left = sorted(original, key=lambda s: s["ts"])
    right = sorted(replacement, key=lambda s: s["ts"])
    matched, diffs, i, j = 0, [], 0, 0
    while i < len(left) and j < len(right):
        delta = left[i]["ts"] - right[j]["ts"]
        if abs(delta) <= MATCH_WINDOW_S:
            matched, i, j = matched + 1, i + 1, j + 1
        elif delta < 0:
            diffs.append({"ts": left[i]["ts"], "side": "original_only", "detail": left[i]["trigger"]})
            i += 1
        else:
            diffs.append({"ts": right[j]["ts"], "side": "replacement_only", "detail": right[j]["trigger"]})
            j += 1
    diffs += [{"ts": s["ts"], "side": "original_only", "detail": s["trigger"]} for s in left[i:]]
    diffs += [{"ts": s["ts"], "side": "replacement_only", "detail": s["trigger"]} for s in right[j:]]
    diffs.sort(key=lambda d: d["ts"], reverse=True)
    return matched, diffs


class Migration:
    def __init__(self, settings, store, feed, ha, lifecycle, orchestrator, safety) -> None:
        self.settings, self.store, self.feed, self.ha = settings, store, feed, ha
        self.lifecycle, self.orchestrator, self.safety = lifecycle, orchestrator, safety

    # --- reading ----------------------------------------------------------------------------
    def _row(self, row: dict) -> dict:
        state = self.ha.states.get(row["entity_id"]) if row["entity_id"] else None
        enabled = None if row["enabled"] is None else bool(row["enabled"])
        if row["source"] == "ha_automation" and state and state.get("state") in ("on", "off"):
            enabled = state["state"] == "on"
        proposals = self.store.query(
            "SELECT id, status, ha_id FROM proposals WHERE replaces_original=? "
            f"AND status NOT IN ({', '.join('?' * len(DEAD))}) ORDER BY created DESC LIMIT 1",
            (row["id"], *DEAD),
        )
        proposal = proposals[0] if proposals else None
        return {
            "id": row["id"],
            "source": row["source"],
            "entity_id": row["entity_id"],
            "title": row["title"],
            "enabled": enabled,
            "summary": row["summary"],
            "config": json.loads(row["config"]) if row["config"] else None,
            "replacement_proposal_id": proposal["id"] if proposal else None,
            "replacement_ha_id": proposal["ha_id"] if proposal and proposal["status"] == "deployed" else None,
            "disabled_by_us": bool(row["disabled_by_us"]),
        }

    def list(self) -> list[dict]:
        return [self._row(r) for r in self.store.query("SELECT * FROM originals ORDER BY source, title, id")]

    def get(self, original_id: str) -> dict:
        rows = self.store.query("SELECT * FROM originals WHERE id=?", (original_id,))
        if not rows:
            raise MigrationError("not_found", f"no original {original_id!r}; run the import first")
        return self._row(rows[0])

    # --- import -----------------------------------------------------------------------------
    async def import_originals(self) -> dict:
        """Read everything again. Reads only: HA states + config GETs, files in the share."""
        found: dict[str, dict] = {}
        skipped: list[str] = []
        scanned: list[str] = []
        if self.ha.connected:
            scanned += ["ha_automation", "ha_script"]
            for entity_id, state in sorted(self.ha.states.items()):
                domain, object_id = entity_id.split(".", 1)
                attrs = state.get("attributes") or {}
                onoff = state.get("state") if state.get("state") in ("on", "off") else None
                title = str(attrs.get("friendly_name") or entity_id)[:120]
                if domain == "automation":
                    ref = str(attrs.get("id") or "")
                    if ref.startswith(AUTOMATION_PREFIX):
                        continue  # ours
                    config = await self._fetch(self.ha.get_automation, ref, skipped) if ref else None
                    found[f"auto_{_slug(ref or object_id)}"] = {
                        "source": "ha_automation",
                        "ref": ref or object_id,
                        "entity_id": entity_id,
                        "title": title,
                        "enabled": None if onoff is None else onoff == "on",
                        "config": normalize(config) if isinstance(config, dict) else None,
                    }
                elif domain == "script":
                    config = await self._fetch(self.ha.get_script, object_id, skipped)
                    found[f"script_{_slug(object_id)}"] = {
                        "source": "ha_script",
                        "ref": object_id,
                        "entity_id": entity_id,
                        "title": title,
                        "enabled": None,
                        "config": config if isinstance(config, dict) else None,
                    }
        else:
            skipped.append("Home Assistant is not connected: automations and scripts were not read")
        folder = self.settings.share_dir / "nodered"
        if folder.is_dir():
            scanned.append("node_red")
            for file in sorted(folder.glob("*.json")):
                try:
                    if file.stat().st_size > MAX_FLOW_BYTES:
                        raise ValueError("larger than 5 MB")
                    flows = parse_flows(file.read_text(encoding="utf-8"), _slug(file.stem))
                except (OSError, ValueError) as err:
                    skipped.append(f"{file.name}: {err}"[:200])
                    continue
                for flow in flows:
                    found[f"nr_{_slug(flow['key'])}"] = {
                        "source": "node_red",
                        "ref": flow["key"],
                        "entity_id": None,
                        "title": flow["title"],
                        "enabled": flow["enabled"],
                        "config": flow["config"],
                    }
        else:
            skipped.append(f"no Node-RED exports: {folder} does not exist")
        known = {r["id"]: r for r in self.store.query("SELECT * FROM originals")}
        imported = updated = 0
        for original_id, item in found.items():
            config = json.dumps(item["config"], ensure_ascii=False) if item["config"] is not None else None
            enabled = None if item["enabled"] is None else int(item["enabled"])
            old = known.get(original_id)
            if old is None:
                imported += 1
            elif (old["title"], old["config"], old["enabled"]) != (item["title"], config, enabled):
                updated += 1
            self.store.execute(
                "INSERT INTO originals (id, source, ref, entity_id, title, enabled, summary, config, "
                "updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
                "ref=excluded.ref, entity_id=excluded.entity_id, title=excluded.title, "
                "enabled=excluded.enabled, summary=excluded.summary, config=excluded.config, "
                "updated=excluded.updated",
                (
                    original_id, item["source"], item["ref"], item["entity_id"], item["title"], enabled,
                    summarize(item["config"]), config, time.time(),
                ),
            )  # fmt: skip
        # Gone from HA or from the share: forget it, unless we are the one who switched it off.
        for original_id, old in known.items():
            if original_id not in found and old["source"] in scanned and not old["disabled_by_us"]:
                self.store.execute("DELETE FROM originals WHERE id=?", (original_id,))
        self.feed.add("system", f"Migration import: {imported} new, {updated} changed, {len(found)} in total")
        return {"imported": imported, "updated": updated, "total": len(found), "skipped": skipped}

    @staticmethod
    async def _fetch(getter, ref: str, skipped: list[str]):
        try:
            return await getter(ref)
        except HAError as err:
            skipped.append(f"{ref}: {err}"[:200])
            return None

    # --- propose ----------------------------------------------------------------------------
    def propose(self, original_id: str) -> str:
        """Queue an orchestrator draft that replaces this original; return the draft's run id."""
        original = self.get(original_id)
        if self.safety.kill_engaged:
            raise MigrationError("kill_switch", "the kill switch is engaged")
        if original["config"] is None:
            raise MigrationError(
                "conflict", "Home Assistant gave no config for this one (YAML-only?): nothing to translate"
            )
        text = yaml.safe_dump(original["config"], sort_keys=False, allow_unicode=True, width=100)
        request = (
            f"Write one automation that does the same as this existing {LABEL[original['source']]} "
            f'"{original["title"]}". {original["summary"] or ""}\n'
            "Keep only what a Home Assistant automation can do with the entities you find; say in the "
            "rationale what the original did that yours does not.\nOriginal:\n"
        )
        request += text[: max(0, 1990 - len(request))]
        run_id = new_id("r")

        async def job() -> None:
            try:
                await self.orchestrator.draft(request, replaces_original=original_id, new_run_id=run_id)
            except OrchestratorError as err:
                self.feed.add(
                    "proposal",
                    f"Migration: no replacement drafted for {original['title']} ({err})"[:300],
                    level="warning",
                    run_id=run_id,
                )
            except Exception:  # noqa: BLE001
                log.exception("migration draft failed")

        self.orchestrator.spawn(job(), f"migrate-{original_id}")
        return run_id

    # --- parity -----------------------------------------------------------------------------
    async def parity(self, original_id: str, days: int = 7) -> dict:
        original = self.get(original_id)
        if not original["replacement_proposal_id"]:
            raise MigrationError("conflict", "no replacement proposal yet: ask for one first")
        proposal = self.lifecycle.get(original["replacement_proposal_id"])
        replacement, _ = self.lifecycle.parse(proposal["yaml"])
        if replacement is None:
            raise MigrationError("conflict", "the replacement's YAML does not parse")
        days = max(1, min(int(days), 30))
        now = None
        try:
            old, old_notes = await firings(original["config"] or {}, self.ha, days, now)
            new, new_notes = await firings(replacement, self.ha, days, now)
        except HAError as err:
            raise MigrationError("ha_error", f"history: {err}"[:300]) from err
        matched, diffs = match(old, new)
        has_triggers = bool((original["config"] or {}).get("triggers"))
        return {
            "days": days,
            "original_fired": len(old),
            "replacement_would_fire": len(new),
            "matched": matched,
            "supported": has_triggers and fully_supported(old_notes) and fully_supported(new_notes),
            "unsupported": [f"original {n}" for n in old_notes] + [f"replacement {n}" for n in new_notes],
            "diffs": diffs[:MAX_DIFFS],
            "simulated": True,  # both sides are trigger replays; conditions are not evaluated
        }

    # --- the reversible "disable original" ----------------------------------------------------
    async def set_enabled(self, original_id: str, enabled: bool) -> dict:
        original = self.get(original_id)
        entity_id = original["entity_id"]
        if original["source"] != "ha_automation" or not entity_id or entity_id not in self.ha.states:
            raise MigrationError(
                "conflict",
                "only an automation Home Assistant knows can be switched from here; "
                "switch scripts and Node-RED flows in their own editor",
            )
        if not enabled and self.safety.kill_engaged:
            raise MigrationError("kill_switch", "the kill switch is engaged: nothing is disabled")
        try:
            await self.ha.set_original_enabled(entity_id, enabled)
        except HAError as err:
            raise MigrationError("ha_error", f"Home Assistant: {err}"[:400]) from err
        self.store.execute(
            "UPDATE originals SET enabled=?, disabled_by_us=? WHERE id=?",
            (int(enabled), int(not enabled), original_id),
        )
        self.feed.add(
            "deploy",
            f"{'Re-enabled' if enabled else 'Disabled'} your automation {entity_id} ({original['title']})",
            level="warning",
            entity_id=entity_id,
        )
        return {**self.get(original_id), "enabled": enabled}
