"""Automation lifecycle: propose → checks → diff → approve → deploy → version → rollback.

A proposal is a draft in our database; it reaches Home Assistant only through `approve` (or
`rollback` to a version the owner approved before), and only under its own `agent_house_<slug>`
id. The four checks run on every create, edit, approve and rollback:

1. yaml        — parses to an object;
2. schema      — an automation (alias, triggers, actions; known keys only), plural keys;
3. policy      — every action names its entities explicitly; each is known, not T3, not the
                 kill-switch helper, of the service's domain, and the service is allowed by the
                 policy (or is a notification). No events, no scripts, no automation/homeassistant
                 services, no templated targets, no area/device/label targets, no webhook triggers;
4. ha_validate — Home Assistant's own `validate_config`.

Approving a proposal counts as the owner's approval for its T2 entities (a human reviewed the
whole automation); T3 can never be deployed. The kill switch blocks approve, rollback and
enable; removing or disabling one of our automations stays possible (it only makes things safer).
The owner's own automations are never written: the HA client refuses any other id.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import time

import yaml

from ..ha.client import AUTOMATION_PREFIX, HAError, guard_ours
from .actuator import new_id
from .policy import resolve_tier

log = logging.getLogger(__name__)

TOP_KEYS = frozenset(
    {
        "alias",
        "description",
        "triggers",
        "conditions",
        "actions",
        "mode",
        "max",
        "max_exceeded",
        "variables",
        "trigger_variables",
    }
)
SINGULAR = {"trigger": "triggers", "condition": "conditions", "action": "actions"}
MODES = frozenset({"single", "restart", "queued", "parallel"})
NOTIFY_SERVICES = re.compile(r"^(notify\.[a-z0-9_]+|persistent_notification\.create)$")
SAFE_STEPS = frozenset({"delay", "wait_template", "wait_for_trigger", "condition", "stop", "variables"})
TARGET_REFS = ("area_id", "device_id", "floor_id", "label_id")
SERVICE_STEP_KEYS = frozenset(
    {"action", "target", "data", "entity_id", "alias", "enabled", "continue_on_error"}
)
BLOCKED_TRIGGERS = frozenset({"webhook"})
OPEN = ("draft", "blocked")
TEMPLATE = re.compile(r"{[{%]")
SERVICE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
ENTITY = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


class LifecycleError(Exception):
    """`code`: not_found | conflict | kill_switch | blocked | ha_error (the API maps them)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:48] or "automation"


def normalize(config: dict) -> dict:
    """Singular keys → plural (HA 2024.10+ form); trigger `platform:` → `trigger:`; `service:` → `action:`."""
    out = {}
    for key, value in config.items():
        key = SINGULAR.get(key, key)
        if key in ("triggers", "conditions", "actions") and not isinstance(value, list):
            value = [value]
        out[key] = value
    out["triggers"] = [
        {("trigger" if k == "platform" else k): v for k, v in t.items()} if isinstance(t, dict) else t
        for t in out.get("triggers") or []
    ]
    out["actions"] = [_action_key(a) for a in out.get("actions") or []]
    return out


def _action_key(step):
    if isinstance(step, dict) and "service" in step and "action" not in step:
        return {("action" if k == "service" else k): v for k, v in step.items()}
    return step


def to_yaml(config: dict) -> str:
    return yaml.safe_dump(config, sort_keys=False, allow_unicode=True, width=100)


class Lifecycle:
    def __init__(self, store, feed, ha, safety, protected=lambda: set()) -> None:
        self.store, self.feed, self.ha, self.safety = store, feed, ha, safety
        self.protected = protected

    # --- checks -----------------------------------------------------------------------------
    def parse(self, text: str) -> tuple[dict | None, list[dict]]:
        """(normalized config or None, [yaml, schema, policy] checks)."""
        try:
            loaded = yaml.safe_load(text)
        except yaml.YAMLError as err:
            return None, [{"name": "yaml", "ok": False, "detail": str(err).splitlines()[0][:300]}]
        if isinstance(loaded, list) and len(loaded) == 1:
            loaded = loaded[0]  # an automations.yaml excerpt
        if not isinstance(loaded, dict):
            return None, [
                {"name": "yaml", "ok": False, "detail": "the YAML must be one automation (an object)"}
            ]
        checks = [{"name": "yaml", "ok": True, "detail": None}]
        config = normalize(loaded)
        problems = self._schema(config)
        checks.append({"name": "schema", "ok": not problems, "detail": "; ".join(problems) or None})
        problems, notes = self._policy(config) if not problems else (["schema errors first"], [])
        checks.append(
            {"name": "policy", "ok": not problems, "detail": "; ".join(problems or notes)[:600] or None}
        )
        return config, checks

    @staticmethod
    def _schema(config: dict) -> list[str]:
        problems = []
        extra = sorted(set(config) - TOP_KEYS)
        if extra:
            problems.append(
                f"unknown key {extra[0]!r}" + (" (Agent House sets the id)" if extra[0] == "id" else "")
            )
        if not isinstance(config.get("alias"), str) or not config["alias"].strip():
            problems.append("alias is required")
        for key in ("triggers", "actions"):
            items = config.get(key)
            if not items or not all(isinstance(i, dict) and i for i in items):
                problems.append(f"{key} must be a non-empty list of objects")
        if "conditions" in config and not all(isinstance(c, dict | str) for c in config["conditions"] or []):
            problems.append("conditions must be a list")
        if "mode" in config and config["mode"] not in MODES:
            problems.append(f"mode must be one of {', '.join(sorted(MODES))}")
        return problems

    def _policy(self, config: dict) -> tuple[list[str], list[str]]:
        """(problems that block the proposal, notes for the owner)."""
        problems: list[str] = []
        tiers: dict[str, str] = {}
        for trigger in config.get("triggers") or []:
            if trigger.get("trigger") in BLOCKED_TRIGGERS:
                problems.append(f"{trigger.get('trigger')} triggers are not allowed")
        self._steps(config.get("actions") or [], problems, tiers, "actions")
        notes = []
        t2 = sorted(e for e, t in tiers.items() if t == "T2")
        if t2:
            notes.append(f"T2, approved with this automation: {', '.join(t2)}")
        if tiers and not problems:
            notes.insert(0, f"{len(tiers)} entities, tiers " + ", ".join(sorted(set(tiers.values()))))
        return problems, notes

    def _steps(self, steps, problems: list[str], tiers: dict, where: str) -> None:
        for number, step in enumerate(steps if isinstance(steps, list) else [steps], 1):
            here = f"{where}[{number}]"
            if not isinstance(step, dict):
                problems.append(f"{here}: not an object")
                continue
            step = _action_key(step)
            if "action" in step:
                self._service(step, problems, tiers, here)
            elif "scene" in step:
                self._service(
                    {"action": "scene.turn_on", "target": {"entity_id": step["scene"]}}, problems, tiers, here
                )
            elif "event" in step:
                problems.append(f"{here}: firing events is not allowed")
            elif "choose" in step:
                for option in _as_list(step["choose"]):
                    self._steps((option or {}).get("sequence", []), problems, tiers, f"{here}.choose")
                self._steps(step.get("default", []), problems, tiers, f"{here}.default")
            elif "if" in step:
                self._steps(step.get("then", []), problems, tiers, f"{here}.then")
                self._steps(step.get("else", []), problems, tiers, f"{here}.else")
            elif "sequence" in step:
                self._steps(step["sequence"], problems, tiers, f"{here}.sequence")
            elif "parallel" in step:
                for branch in _as_list(step["parallel"]):
                    self._steps(
                        branch.get("sequence", [branch]) if isinstance(branch, dict) else branch,
                        problems,
                        tiers,
                        f"{here}.parallel",
                    )
            elif "repeat" in step:
                self._steps((step["repeat"] or {}).get("sequence", []), problems, tiers, f"{here}.repeat")
            elif not set(step) & SAFE_STEPS:
                problems.append(f"{here}: unknown step {sorted(step)[0]!r}")

    def _service(self, step: dict, problems: list[str], tiers: dict, here: str) -> None:
        service = step.get("action")
        if not isinstance(service, str) or not SERVICE.fullmatch(service):
            problems.append(f"{here}: service {service!r} must be a literal domain.service")
            return
        domain, name = service.split(".", 1)
        extra = sorted(set(step) - SERVICE_STEP_KEYS)
        if extra:  # data_template, target_template, … would name entities the scan cannot see
            problems.append(f"{here}: key {extra[0]!r} is not allowed in a service call")
        for key in ("data", "target"):
            if key in step and not isinstance(step[key], dict):
                problems.append(f"{here}: {key} must be a literal mapping, not a template")
        data = step.get("data") if isinstance(step.get("data"), dict) else {}
        target = step.get("target") if isinstance(step.get("target"), dict) else {}
        if set(target) - {"entity_id"}:
            problems.append(f"{here}: target may only name entity_id")
        refs = [k for k in TARGET_REFS if k in target or k in data or k in step]
        if refs:
            problems.append(f"{here}: target {refs[0]} is not allowed, name the entities")
        entities: list = []
        for source in (target.get("entity_id"), data.get("entity_id"), step.get("entity_id")):
            entities += _as_list(source)
        if NOTIFY_SERVICES.fullmatch(service):
            if entities:
                problems.append(f"{here}: {service} takes no entities")
            return
        policy = self.safety.policy
        allowed = (policy.get("allowed_services") or {}).get(domain) or []
        if name not in allowed:
            problems.append(f"{here}: {service} is not an allowed service")
        if not entities:
            problems.append(f"{here}: {service} must name its entity_id")
        protected = set(self.protected())
        for entity_id in entities:
            if (
                not isinstance(entity_id, str)
                or TEMPLATE.search(entity_id)
                or not ENTITY.fullmatch(entity_id)
            ):
                problems.append(f"{here}: entity {entity_id!r} must be a literal entity id")
                continue
            if entity_id.split(".", 1)[0] != domain:
                problems.append(f"{here}: {entity_id} is not a {domain} entity")
            if entity_id not in self.ha.states:
                problems.append(f"{here}: {entity_id} is unknown to Home Assistant")
            if entity_id in protected:
                problems.append(f"{here}: {entity_id} is the kill-switch helper")
            tier = resolve_tier(entity_id, policy)
            tiers[entity_id] = tier
            if tier == "T3":
                problems.append(f"{here}: {entity_id} is T3 and can never be automated")

    async def _ha_check(self, config: dict) -> dict:
        if not self.ha.connected:
            return {"name": "ha_validate", "ok": False, "detail": "Home Assistant is not connected"}
        try:
            result = await self.ha.validate_automation(config)
        except HAError as err:
            return {"name": "ha_validate", "ok": False, "detail": f"{err}"[:300]}
        errors = [f"{k}: {v.get('error')}" for k, v in (result or {}).items() if not (v or {}).get("valid")]
        return {"name": "ha_validate", "ok": not errors, "detail": "; ".join(errors)[:300] or None}

    async def check(self, text: str) -> tuple[dict | None, list[dict]]:
        config, checks = self.parse(text)
        if config is not None and all(c["ok"] for c in checks):
            checks.append(await self._ha_check(config))
        else:
            checks.append({"name": "ha_validate", "ok": False, "detail": "skipped: fix the checks above"})
        return config, checks

    # --- proposals --------------------------------------------------------------------------
    def _row(self, row: dict) -> dict:
        out = {**row, "checks": json.loads(row["checks"] or "[]")}
        out["evidence"] = json.loads(row["evidence"]) if row.get("evidence") else None
        out["diff"] = self._diff(row["ha_id"], row["yaml"]) if row["status"] in OPEN else None
        out.pop("updated", None)
        return out

    def get(self, proposal_id: str) -> dict:
        rows = self.store.query("SELECT * FROM proposals WHERE id=?", (proposal_id,))
        if not rows:
            raise LifecycleError("not_found", f"no proposal {proposal_id!r}")
        return self._row(rows[0])

    def list(self, status: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM proposals", ()
        if status:
            sql, params = sql + " WHERE status=?", (status,)
        return [self._row(r) for r in self.store.query(sql + " ORDER BY created DESC LIMIT 200", params)]

    def count_open(self) -> int:
        return self.store.query("SELECT COUNT(*) AS n FROM proposals WHERE status='draft'")[0]["n"]

    def _deployed(self, ha_id: str) -> dict | None:
        rows = self.store.query("SELECT * FROM deployed WHERE ha_id=? AND removed=0", (ha_id,))
        return rows[0] if rows else None

    def _version(self, ha_id: str, version: int) -> dict | None:
        rows = self.store.query(
            "SELECT * FROM automation_versions WHERE ha_id=? AND version=?", (ha_id, version)
        )
        return rows[0] if rows else None

    def _diff(self, ha_id: str, text: str) -> str:
        current = self._deployed(ha_id)
        before = self._version(ha_id, current["current_version"]) if current else None
        old = before["yaml"] if before else ""
        label = f"{ha_id} v{current['current_version']}" if current else "(not deployed)"
        return "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True), text.splitlines(keepends=True), label, "proposal"
            )
        )

    def _new_ha_id(self, title: str) -> str:
        base = AUTOMATION_PREFIX + slugify(title)
        taken = {r["ha_id"] for r in self.store.query("SELECT ha_id FROM deployed")}
        taken |= {
            r["ha_id"]
            for r in self.store.query("SELECT ha_id FROM proposals WHERE status IN ('draft', 'blocked')")
        }
        ha_id, n = base, 1
        while ha_id in taken:
            n += 1
            ha_id = f"{base}_{n}"
        return guard_ours(ha_id)

    async def create(
        self,
        text: str,
        *,
        title: str | None = None,
        rationale: str | None = None,
        expected_triggers: str | None = None,
        origin_agent: str | None = None,
        run_id: str | None = None,
        ha_id: str | None = None,
    ) -> dict:
        """A new draft. `ha_id` = one of our deployed automations to change; else a new id."""
        config, checks = await self.check(text)
        title = (title or (config or {}).get("alias") or "Untitled automation").strip()[:120]
        if ha_id is not None:
            guard_ours(ha_id)
            if not self._deployed(ha_id):
                raise LifecycleError("not_found", f"{ha_id} is not one of our deployed automations")
        else:
            ha_id = self._new_ha_id(title)
        now = time.time()
        proposal = {
            "id": new_id("p"),
            "created": now,
            "updated": now,
            "status": "draft" if all(c["ok"] for c in checks) else "blocked",
            "title": title,
            "yaml": to_yaml(config) if config is not None else text,
            "rationale": rationale,
            "expected_triggers": expected_triggers,
            "origin_agent": origin_agent,
            "run_id": run_id,
            "ha_id": ha_id,
            "checks": json.dumps(checks),
        }
        self.store.execute(
            "INSERT INTO proposals (id, created, updated, status, title, yaml, rationale, expected_triggers, "
            "origin_agent, run_id, ha_id, checks) VALUES (:id, :created, :updated, :status, :title, :yaml, "
            ":rationale, :expected_triggers, :origin_agent, :run_id, :ha_id, :checks)",
            proposal,
        )
        self.feed.add(
            "proposal",
            f"Proposal {proposal['status']}: {title}",
            level="info",
            agent_id=origin_agent,
            run_id=run_id,
            data={"proposal_id": proposal["id"], "ha_id": ha_id},
        )
        return self.get(proposal["id"])

    def _open(self, proposal_id: str) -> dict:
        proposal = self.get(proposal_id)
        if proposal["status"] not in OPEN:
            raise LifecycleError("conflict", f"the proposal is {proposal['status']}")
        return proposal

    async def edit(self, proposal_id: str, text: str) -> dict:
        self._open(proposal_id)
        config, checks = await self.check(text)
        self.store.execute(
            "UPDATE proposals SET yaml=?, checks=?, status=?, evidence=NULL, updated=? WHERE id=?",
            (
                to_yaml(config) if config is not None else text,
                json.dumps(checks),
                "draft" if all(c["ok"] for c in checks) else "blocked",
                time.time(),
                proposal_id,
            ),
        )
        return self.get(proposal_id)

    def reject(self, proposal_id: str, note: str | None = None) -> dict:
        self._open(proposal_id)
        self.store.execute(
            "UPDATE proposals SET status='rejected', note=?, updated=? WHERE id=?",
            ((note or "")[:500] or None, time.time(), proposal_id),
        )
        self.feed.add("proposal", f"Proposal rejected: {self.get(proposal_id)['title']}")
        return self.get(proposal_id)

    async def evidence(self, proposal_id: str, days: int = 7) -> dict:
        from .evidence import simulate  # local: keeps the lifecycle importable without history code

        proposal = self.get(proposal_id)
        config, checks = self.parse(proposal["yaml"])
        if config is None or not checks[1]["ok"]:
            raise LifecycleError("blocked", "the YAML is not a valid automation yet")
        result = await simulate(config, self.ha, days)
        self.store.execute("UPDATE proposals SET evidence=? WHERE id=?", (json.dumps(result), proposal_id))
        return result

    # --- deploy -----------------------------------------------------------------------------
    def _no_kill(self, what: str) -> None:
        if self.safety.kill_engaged:
            raise LifecycleError("kill_switch", f"the kill switch is engaged: no {what}")

    async def _write(self, ha_id: str, config: dict, note: str) -> None:
        """The only place a config goes to HA. Our id, never somebody else's automation."""
        self._no_kill("deploys")
        guard_ours(ha_id)
        existing = await self.ha.get_automation(ha_id)
        if existing is not None and not self.store.query("SELECT 1 FROM deployed WHERE ha_id=?", (ha_id,)):
            raise LifecycleError("conflict", f"{ha_id} exists in Home Assistant but was not deployed by us")
        body = {**config, "description": f"{config.get('description') or ''}\n[Agent House: {note}]".strip()}
        try:
            await self.ha.write_automation(ha_id, body)
        except HAError as err:
            raise LifecycleError("ha_error", f"Home Assistant refused the automation: {err}"[:400]) from err

    def _record(
        self, ha_id: str, title: str, text: str, proposal_id: str | None, note: str, origin=None, run_id=None
    ) -> int:
        rows = self.store.query("SELECT MAX(version) AS v FROM automation_versions WHERE ha_id=?", (ha_id,))
        version = (rows[0]["v"] or 0) + 1
        self.store.execute(
            "INSERT INTO automation_versions (ha_id, version, created, yaml, proposal_id, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ha_id, version, time.time(), text, proposal_id, note),
        )
        self.store.execute(
            "INSERT INTO deployed (ha_id, title, current_version, enabled, removed, origin_agent, run_id) "
            "VALUES (?, ?, ?, 1, 0, ?, ?) ON CONFLICT (ha_id) DO UPDATE SET title=excluded.title, "
            "current_version=excluded.current_version, enabled=1, removed=0",
            (ha_id, title, version, origin, run_id),
        )
        return version

    async def approve(self, proposal_id: str) -> dict:
        proposal = self._open(proposal_id)
        self._no_kill("deploys")
        if proposal["status"] != "draft":
            raise LifecycleError("conflict", "the proposal is blocked: fix the failed checks first")
        # Claim it: a double click must not deploy twice.
        claimed = self.store.execute(
            "UPDATE proposals SET status='approved', updated=? WHERE id=? AND status='draft'",
            (time.time(), proposal_id),
        )
        if claimed.rowcount != 1:
            raise LifecycleError("conflict", "the proposal is already being approved")
        try:
            # Policy, entities or HA may have changed since the draft: check again, as of now.
            config, checks = await self.check(proposal["yaml"])
            if config is None or not all(c["ok"] for c in checks):
                self.store.execute(
                    "UPDATE proposals SET status='blocked', checks=? WHERE id=?",
                    (json.dumps(checks), proposal_id),
                )
                raise LifecycleError("blocked", "a check failed on approval; nothing was deployed")
            await self._write(proposal["ha_id"], config, f"proposal {proposal_id}")
        except BaseException:
            self.store.execute(
                "UPDATE proposals SET status='draft' WHERE id=? AND status='approved'", (proposal_id,)
            )
            raise
        previous = self._deployed(proposal["ha_id"])
        version = self._record(
            proposal["ha_id"],
            proposal["title"],
            proposal["yaml"],
            proposal_id,
            "approved by the owner",
            proposal["origin_agent"],
            proposal["run_id"],
        )
        if previous:
            self.store.execute(
                "UPDATE proposals SET status='superseded' WHERE ha_id=? AND status='deployed'",
                (proposal["ha_id"],),
            )
        self.store.execute(
            "UPDATE proposals SET status='superseded', note='another version was deployed' "
            "WHERE ha_id=? AND status IN ('draft', 'blocked') AND id<>?",
            (proposal["ha_id"], proposal_id),
        )
        self.store.execute(
            "UPDATE proposals SET status='deployed', updated=? WHERE id=?", (time.time(), proposal_id)
        )
        self.feed.add(
            "deploy",
            f"Deployed {proposal['ha_id']} v{version}: {proposal['title']}",
            level="warning",
            data={"ha_id": proposal["ha_id"], "version": version, "proposal_id": proposal_id},
        )
        return self.get(proposal_id)

    # --- deployed automations ---------------------------------------------------------------
    def deployed(self, ha_id: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM deployed WHERE removed=0", ()
        if ha_id:
            sql, params = sql + " AND ha_id=?", (ha_id,)
        out = []
        for row in self.store.query(sql + " ORDER BY ha_id", params):
            entity = self.ha.automation_entity(row["ha_id"])
            state = (self.ha.states.get(entity) or {}).get("state") if entity else None
            versions = self.store.query(
                "SELECT version, created, yaml, proposal_id, note FROM automation_versions WHERE ha_id=? "
                "ORDER BY version DESC",
                (row["ha_id"],),
            )
            out.append(
                {
                    "ha_id": row["ha_id"],
                    "entity_id": entity,
                    "title": row["title"],
                    "current_version": row["current_version"],
                    "enabled": state == "on" if state in ("on", "off") else bool(row["enabled"]),
                    "present_in_ha": entity is not None,
                    "origin_agent": row["origin_agent"],
                    "run_id": row["run_id"],
                    "versions": versions,
                }
            )
        return out

    def get_deployed(self, ha_id: str) -> dict:
        found = self.deployed(ha_id)
        if not found:
            raise LifecycleError("not_found", f"{ha_id} is not one of our deployed automations")
        return found[0]

    def count_deployed(self) -> int:
        return self.store.query("SELECT COUNT(*) AS n FROM deployed WHERE removed=0")[0]["n"]

    async def rollback(self, ha_id: str, version: int) -> dict:
        current = self.get_deployed(ha_id)
        self._no_kill("rollbacks")
        target = self._version(ha_id, int(version))
        if target is None:
            raise LifecycleError("not_found", f"{ha_id} has no version {version}")
        if int(version) == current["current_version"]:
            raise LifecycleError("conflict", f"v{version} is already deployed")
        config, checks = await self.check(target["yaml"])
        if config is None or not all(c["ok"] for c in checks):
            failed = "; ".join(f"{c['name']}: {c['detail']}" for c in checks if not c["ok"])
            raise LifecycleError("blocked", f"v{version} no longer passes the checks: {failed}"[:500])
        await self._write(ha_id, config, f"rollback to v{version}")
        old = self._version(ha_id, current["current_version"])
        new = self._record(
            ha_id, current["title"], target["yaml"], target["proposal_id"], f"rollback to v{version}"
        )
        if old and old["proposal_id"]:
            self.store.execute("UPDATE proposals SET status='rolled_back' WHERE id=?", (old["proposal_id"],))
        self.feed.add(
            "deploy",
            f"Rolled back {ha_id} to v{version} (now v{new})",
            level="warning",
            data={"ha_id": ha_id, "version": new},
        )
        return self.get_deployed(ha_id)

    async def remove(self, ha_id: str) -> None:
        self.get_deployed(ha_id)
        try:
            await self.ha.delete_automation(ha_id)
        except HAError as err:
            if "HTTP 404" not in str(err):
                raise LifecycleError("ha_error", f"Home Assistant: {err}"[:400]) from err
        self.store.execute("UPDATE deployed SET removed=1 WHERE ha_id=?", (ha_id,))
        self.feed.add("deploy", f"Removed {ha_id} from Home Assistant (versions kept)", level="warning")

    async def set_enabled(self, ha_id: str, enabled: bool) -> dict:
        self.get_deployed(ha_id)
        if enabled:
            self._no_kill("enabling automations")
        try:
            await self.ha.set_automation_enabled(ha_id, enabled)
        except HAError as err:
            raise LifecycleError("ha_error", f"Home Assistant: {err}"[:400]) from err
        self.store.execute("UPDATE deployed SET enabled=? WHERE ha_id=?", (int(enabled), ha_id))
        self.feed.add("deploy", f"{'Enabled' if enabled else 'Disabled'} {ha_id}", level="info")
        return self.get_deployed(ha_id)


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]
