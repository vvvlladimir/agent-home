"""Daily house review (M5): once a day the orchestrator reads what happened and proposes.

1. History scan (core/insights.py, deterministic): habits, undone automations, conflicts.
2. A digest of the last 24 h from local data: runs and escalations per agent, the owner's inbox,
   the board's escalations and reports, open proposals, the scan's findings.
3. The digest is summarised for the owner by the local model (`routine`); without a model the
   digest itself is the review. It goes to the inbox as a report of agent `orchestrator`.
4. New findings become proposals, at most `MAX_DRAFTS` per review, conflicts first, then undone
   automations, then habits: one proposal per finding, so never two drafts for one conflict.
   One proposal per entity: a finding is skipped (it stays `new` for a later review) when an
   earlier finding of this review targets the same entity, or when an open proposal of ours
   (draft, blocked, being approved) already acts on it.
   A conflict between two of our automations is drafted as a change of one that supersedes the
   other (switched off on approval); the owner's automations and scripts are never touched.
   An undone automation that should rather be rolled back says so in its inbox line; nothing is
   rolled back or deployed here.

First look (D31): the owner should not wait a day. Once Home Assistant is connected, the add-on
scans the history once (`habit_days`) and drafts up to `FIRST_DRAFTS` proposals from it right
away; failures retry every `FIRST_RETRY_S`. After that, the daily review does the same each day.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from . import habits
from .insights import _targets
from .lifecycle import action_entities
from .llm import Priority
from .metrics import is_weak
from .orchestrator import OrchestratorError

log = logging.getLogger(__name__)

MAX_DRAFTS = 3
FIRST_DRAFTS = 5
FIRST_RETRY_S = 600.0
FIRST_KEY = "habits_first_look"
PENDING = ("draft", "blocked", "approved")  # proposals that may still reach HA
ORDER = {"conflict": 0, "undo": 1, "habit": 2}
LATE_LIMIT = timedelta(hours=2)  # a review missed by more (add-on was down) waits for the next day


class Review:
    def __init__(self, store, feed, orchestrator, insights, council, agents_rt, settings, lifecycle) -> None:
        self.store, self.feed, self.orch, self.insights = store, feed, orchestrator, insights
        self.council, self.rt, self.settings, self.lifecycle = council, agents_rt, settings, lifecycle
        self.running = False
        self._first_tried = 0.0
        self.metrics = None  # core/metrics.py, wired by the app factory

    def due(self, now: datetime) -> bool:
        at = getattr(self.settings, "daily_review_at", "") or ""
        if not at or self.running:
            return False
        hour, minute = (int(p) for p in at.split(":"))
        slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return slot <= now < slot + LATE_LIMIT and self.store.get("review_last_day") != now.date().isoformat()

    def maybe_first_look(self) -> bool:
        """From the council tick: start the first look in the background, once HA is connected."""
        if self.store.get(FIRST_KEY) or self.running or self.orch.safety.kill_engaged:
            return False
        if not getattr(self.orch.ha, "connected", False) or time.time() - self._first_tried < FIRST_RETRY_S:
            return False
        self.orch.spawn(self.first_look(), "first-look")
        return True

    async def first_look(self) -> dict | None:
        """The first scan and drafts (see the module doc). None when it has to try again later."""
        if self.running:
            return None
        self._first_tried = time.time()
        self.running = True
        try:
            scan = await self.insights.scan()
            drafted = await self.draft_new(FIRST_DRAFTS)
        except Exception as err:  # noqa: BLE001 - the logbook may not be ready yet: try again later
            log.info("first look failed: %s", err)
            return None
        finally:
            self.running = False
        funnel = scan.get("habits") or {}
        self.store.set(FIRST_KEY, {"ts": time.time(), "drafted": len(drafted), "habits": funnel})
        text = (
            f"First look at your last {funnel.get('days', '?')} days: {funnel.get('relevant', 0)} of "
            f"{funnel.get('entities', 0)} entities are devices you switch by hand often enough, "
            f"{funnel.get('dossiers', 0)} of them regularly."
        )
        if drafted:
            text += " Proposed: " + "; ".join(d["title"] for d in drafted) + ". See Proposals."
        else:
            text += " Nothing regular enough to propose yet; the daily review looks again every day."
        self.rt.inbox_add("orchestrator", None, "info", text[:2000])
        return {"drafted": drafted, "habits": funnel}

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
        self.council.wake_fired("daily_review")
        try:
            try:
                scan = await self.insights.scan()
            except Exception as err:  # noqa: BLE001 - no history is no reason to skip the review
                log.warning("history scan failed: %s", err)
                scan = {"findings": [], "feedback": [], "error": str(err)[:200]}
            accuracy = await self._accuracy(now)
            digest = self.digest(now, scan) + accuracy
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

    async def _accuracy(self, now: datetime) -> str:
        """The accuracy metrics (D28): one line for the digest, and a snapshot for the trend."""
        if self.metrics is None:
            return ""
        try:
            result = await self.metrics.compute()
        except Exception as err:  # noqa: BLE001 - the logbook may be unreachable
            log.info("metrics unavailable: %s", err)
            return ""
        self.metrics.snapshot(now.date().isoformat(), result)
        p = result["program"]
        lines = [f"\nAccuracy (7 days): score {p['score']} (lower bound {p['score_low']}), "
                 f"precision {p['precision']['value']}, coverage {p['coverage']['value']}, "
                 f"house share {p['house_share']['value']}"]  # fmt: skip
        weak = [a for a in result["automations"] if is_weak(a)]
        lines += [f"Weak: {a['ha_id']} f1 {a['f1']} ({', '.join(a['examples'][:2])})" for a in weak[:5]]
        return "\n".join(lines)

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

    def targets(self, finding: dict) -> set[str]:
        """The entities a finding's proposal would act on."""
        if finding["kind"] == "undo":
            return _targets(self.lifecycle, finding["ha_id"]) if finding["ha_id"] else set()
        entity_id = finding["data"].get("entity_id")
        return {entity_id} if entity_id else set()

    def pending_entities(self) -> set[str]:
        """Entities that open proposals of ours act on."""
        marks = ",".join("?" * len(PENDING))
        rows = self.store.query(f"SELECT yaml FROM proposals WHERE status IN ({marks})", PENDING)
        return set().union(*(action_entities(r["yaml"]) for r in rows))

    async def draft_new(self, limit: int = MAX_DRAFTS) -> list[dict]:
        """At most `limit` findings, conflicts first; one proposal per entity per review."""
        new = sorted(self.insights.list("new"), key=lambda f: (ORDER.get(f["kind"], 9), -f["updated"]))
        out: list[dict] = []
        taken = self.pending_entities()
        for finding in new:
            if len(out) >= limit:
                break
            targets = self.targets(finding)
            if targets & taken:
                log.info("finding %s waits: %s has a proposal", finding["id"], sorted(targets & taken))
                continue
            taken |= targets
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
        if ha_id and not self.lifecycle.deployed(ha_id):
            ha_id = None
        supersedes = [
            sid for sid in finding["data"].get("supersedes") or []
            if finding["kind"] == "conflict" and sid != ha_id and self.lifecycle.deployed(sid)
        ]  # fmt: skip
        try:
            proposal = await self.orch.draft(
                finding["request"], room=finding["room"] if finding["kind"] == "habit" else None,
                origin_agent="orchestrator", ha_id=ha_id, priority=Priority.BACKGROUND, task=finding["kind"],
                supersedes=supersedes or None,
            )  # fmt: skip
        except Exception as err:  # noqa: BLE001 - OrchestratorError, LifecycleError, …
            self.feed.add(
                "council", f"finding not drafted: {finding['title']} ({err})"[:300], level="warning"
            )
            return self.insights.set_status(finding_id, "failed")
        evidence = _evidence_text(finding, supersedes)
        if evidence:
            self.lifecycle.annotate(proposal["id"], evidence)
        if proposal["status"] in ("draft", "blocked") and finding["kind"] == "habit":
            try:
                await self.lifecycle.evidence(proposal["id"], int(getattr(self.settings, "habit_days", 10)))
            except Exception as err:  # noqa: BLE001 - the replay is a bonus, the draft stands
                log.info("evidence for %s failed: %s", proposal["id"], err)
        return self.insights.set_status(
            finding_id, "drafted", proposal_id=proposal["id"], run_id=proposal["run_id"]
        )


def _evidence_text(finding: dict, supersedes: list[str] | None = None) -> str:
    data = finding["data"]
    if finding["kind"] == "habit" and data.get("states"):
        return f"by hand in the last {data['days_scanned']} days {habits.summary(data)}"
    if finding["kind"] == "undo":
        return f"undone by hand {data['undone']} of {data['acted']} times ({int(data['rate'] * 100)}%)"
    if finding["kind"] == "conflict":
        text = f"switched back and forth {data['count']}× by {' and '.join(data['sides'])}."
        theirs = data.get("theirs") or []
        if supersedes:
            text += (f" Supersedes {', '.join(supersedes)}: switched off when you approve "
                     "(turn it back on from the Deployed page).")  # fmt: skip
        if theirs:
            text += (f" {', '.join(theirs)} is yours and stays untouched; switch it off from the "
                     "Migration page if this automation should decide.")  # fmt: skip
        return text
    return ""
