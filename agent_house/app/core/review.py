"""Daily house review (M5): once a day the orchestrator reads what happened and proposes.

1. History scan (core/insights.py, deterministic): habits, undone automations, conflicts.
2. A digest of the last 24 h from local data: runs and escalations per agent, the owner's inbox,
   the board's escalations and reports, open proposals, the scan's findings.
3. The digest is summarised for the owner by the local model (`routine`); without a model the
   digest itself is the review. It goes to the inbox as a report of agent `orchestrator`.
4. New findings become proposals, at most `MAX_DRAFTS` per review, conflicts first, then undone
   automations, then habits: one proposal per finding, so never two drafts for one conflict.
   An undone automation that should rather be rolled back says so in its inbox line; nothing is
   rolled back or deployed here.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from .llm import Priority
from .orchestrator import OrchestratorError

log = logging.getLogger(__name__)

MAX_DRAFTS = 3
ORDER = {"conflict": 0, "undo": 1, "habit": 2}
LATE_LIMIT = timedelta(hours=2)  # a review missed by more (add-on was down) waits for the next day


class Review:
    def __init__(self, store, feed, orchestrator, insights, council, agents_rt, settings, lifecycle) -> None:
        self.store, self.feed, self.orch, self.insights = store, feed, orchestrator, insights
        self.council, self.rt, self.settings, self.lifecycle = council, agents_rt, settings, lifecycle
        self.running = False

    def due(self, now: datetime) -> bool:
        at = getattr(self.settings, "daily_review_at", "") or ""
        if not at or self.running:
            return False
        hour, minute = (int(p) for p in at.split(":"))
        slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return slot <= now < slot + LATE_LIMIT and self.store.get("review_last_day") != now.date().isoformat()

    async def maybe_run(self, now: datetime) -> dict | None:
        if not self.due(now):
            return None
        return await self.run(now)

    async def run(self, now: datetime | None = None) -> dict:
        if self.running:
            raise OrchestratorError("conflict", "a review is already running")
        if self.orch.safety.kill_engaged:
            raise OrchestratorError("kill_switch", "the kill switch is engaged")
        self.running = True
        now = now or datetime.now().astimezone()
        self.store.set("review_last_day", now.date().isoformat())
        try:
            try:
                scan = await self.insights.scan()
            except Exception as err:  # noqa: BLE001 - no history is no reason to skip the review
                log.warning("history scan failed: %s", err)
                scan = {"findings": [], "feedback": [], "error": str(err)[:200]}
            digest = self.digest(now, scan)
            try:
                text, run_id = await self.orch.summarize(digest)
            except OrchestratorError as err:
                text, run_id = "", None
                log.info("review summary without a model: %s", err)
            drafted = await self.draft_new()
            text = text or digest
            if drafted:
                text += "\nDrafted: " + "; ".join(f"{d['title']} → {d['status']}" for d in drafted)
            for item in scan.get("feedback") or []:
                if item.get("flagged") and any(f.get("suggest_rollback") for f in self._undo_findings(item)):
                    text += f"\nConsider rolling back {item['ha_id']}: undone {int(item['rate'] * 100)}%."
            self.rt.inbox_add("orchestrator", run_id, "info", f"Daily review: {text}"[:2000])
            result = {
                "summary": text,
                "run_id": run_id,
                "drafted": drafted,
                "scan": {k: v for k, v in scan.items() if k != "findings"},
                "findings": len(scan.get("findings") or []),
            }
            self.store.set("review_last", {"ts": time.time(), "summary": text[:2000], "run_id": run_id,
                                           "drafted": len(drafted)})  # fmt: skip
            return result
        finally:
            self.running = False

    def _undo_findings(self, item: dict) -> list[dict]:
        found = self.insights.get(f"undo:{item['ha_id']}:v{item.get('version')}")
        return [found["data"]] if found else []

    def digest(self, now: datetime, scan: dict) -> str:
        since = (now - timedelta(days=1)).timestamp()
        lines = [
            f"Review of the 24 h up to {now:%Y-%m-%d %H:%M}.",
            f"House mode: {self.council.mode or 'unknown'}",
        ]
        runs = self.store.query(
            "SELECT agent_id, status, COUNT(*) AS n FROM runs WHERE started>=? AND replay_of IS NULL "
            "GROUP BY agent_id, status ORDER BY agent_id",
            (since,),
        )
        per_agent: dict[str, list[str]] = {}
        for row in runs:
            per_agent.setdefault(row["agent_id"], []).append(f"{row['n']} {row['status']}")
        if per_agent:
            lines.append("Runs: " + "; ".join(f"{a}: {', '.join(v)}" for a, v in per_agent.items()))
        inbox = self.store.query(
            "SELECT agent_id, severity, summary FROM inbox WHERE ts>=? AND agent_id<>'orchestrator' "
            "ORDER BY id DESC LIMIT 12",
            (since,),
        )
        if inbox:
            lines.append("Agent reports:")
            lines += [f"- {r['agent_id']} [{r['severity']}]: {r['summary'][:160]}" for r in inbox]
        board = [r for r in self.council.board.since(since) if r["kind"] in ("escalation", "report")]
        if board:
            lines.append("Board (escalations and task reports):")
            lines += [f"- {r['agent_id']} [{r['kind']}]: {r['text']}" for r in board[-12:]]
        actions = self.store.query(
            "SELECT verdict, COUNT(*) AS n FROM actions WHERE ts>=? GROUP BY verdict", (since,)
        )
        if actions:
            lines.append("Actions: " + ", ".join(f"{r['n']} {r['verdict']}" for r in actions))
        lines.append(f"Proposals waiting for the owner: {self.lifecycle.count_open()}")
        for item in scan.get("feedback") or []:
            lines.append(
                f"Deployed {item['ha_id']}: acted {item['acted']}×, undone by hand {item['undone']}× "
                f"({int(item['rate'] * 100)}%)"
            )
        findings = scan.get("findings") or []
        if findings:
            lines.append("History scan found:")
            lines += [f"- [{f['kind']}] {f['title']} ({f['status']})" for f in findings[:10]]
        if scan.get("error"):
            lines.append(f"History scan failed: {scan['error']}")
        return "\n".join(lines)

    async def draft_new(self) -> list[dict]:
        new = sorted(self.insights.list("new"), key=lambda f: (ORDER.get(f["kind"], 9), -f["updated"]))
        out = []
        for finding in new[:MAX_DRAFTS]:
            result = await self.draft(finding["id"])
            if result and result.get("proposal_id"):
                status = self.lifecycle.get(result["proposal_id"])["status"]
                out.append({"finding": finding["id"], "title": finding["title"], "status": status})
            elif result:
                out.append({"finding": finding["id"], "title": finding["title"], "status": result["status"]})
        return out

    async def draft(self, finding_id: str) -> dict | None:
        """One finding → one proposal (the finding remembers it, so it is never drafted twice)."""
        finding = self.insights.get(finding_id)
        if finding is None or finding["status"] not in ("new", "failed"):
            return finding
        self.insights.set_status(finding_id, "drafting")
        ha_id = finding["ha_id"] if finding["kind"] in ("undo", "conflict") else None
        if ha_id and not any(d["ha_id"] == ha_id for d in self.lifecycle.deployed(ha_id)):
            ha_id = None
        try:
            proposal = await self.orch.draft(
                finding["request"], room=finding["room"] if finding["kind"] == "habit" else None,
                origin_agent="orchestrator", ha_id=ha_id, priority=Priority.BACKGROUND, task=finding["kind"],
            )  # fmt: skip
        except Exception as err:  # noqa: BLE001 - OrchestratorError, LifecycleError, …
            self.feed.add(
                "council", f"finding not drafted: {finding['title']} ({err})"[:300], level="warning"
            )
            return self.insights.set_status(finding_id, "failed")
        evidence = _evidence_text(finding)
        if evidence:
            self.lifecycle.annotate(proposal["id"], evidence)
        if proposal["status"] in ("draft", "blocked") and finding["kind"] == "habit":
            try:
                await self.lifecycle.evidence(proposal["id"], int(getattr(self.settings, "habit_days", 14)))
            except Exception as err:  # noqa: BLE001 - the replay is a bonus, the draft stands
                log.info("evidence for %s failed: %s", proposal["id"], err)
        return self.insights.set_status(
            finding_id, "drafted", proposal_id=proposal["id"], run_id=proposal["run_id"]
        )


def _evidence_text(finding: dict) -> str:
    data = finding["data"]
    if finding["kind"] == "habit" and data.get("kind") == "follow":
        return (
            f"{data['count']}× on {data['days']} days by hand after {data['trigger']} → "
            f"{data['trigger_state']} (followed {int(data['precision'] * 100)}% of the sensor changes, "
            f"median {data['median_delay_s']} s)"
        )
    if finding["kind"] == "habit":
        return (
            f"{data['count']}× by hand at about {data['at']} on {data['days']} of {data['days_scanned']} days"
        )
    if finding["kind"] == "undo":
        return f"undone by hand {data['undone']} of {data['acted']} times ({int(data['rate'] * 100)}%)"
    if finding["kind"] == "conflict":
        return f"switched back and forth {data['count']}× by {' and '.join(data['sides'])}"
    return ""
