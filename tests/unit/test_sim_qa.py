"""sim/qa: the deterministic part of the coordinator (validation, candidate pairs, grouping)."""

from __future__ import annotations

from sim.qa.run import candidate_pairs, group, render, validate


def finding(title: str, endpoint: str = "/api/manager/wake", severity: str = "major", **extra) -> dict:
    return {"title": title, "kind": "bug", "severity": severity, "steps": ["curl ..."], "expected": "202",
            "actual": f"{title}: 500 Internal Server Error", "endpoint": endpoint, "reproduced_by_me": True,
            **extra}  # fmt: skip


def test_a_finding_needs_every_field_and_a_reproduction():
    assert validate(finding("wake fails")) == []
    assert "not reproduced by the tester" in validate(finding("x", reproduced_by_me=False))
    assert "kind not in" in " ".join(validate(finding("x", kind="vibe")))
    assert validate("text") == ["not an object"]


def test_only_similar_findings_on_the_same_endpoint_are_candidates():
    items = [finding("manager wake returns 500"), finding("manager wake gives 500 error"),
             finding("manager wake returns 500", endpoint="/api/metrics")]  # fmt: skip
    assert candidate_pairs(items) == [(0, 1)]


def test_groups_keep_the_worst_severity_and_all_roles():
    items = [
        finding("a", role="chaos"),
        finding("a", severity="critical", role="intruder"),
        finding("b", role="x"),
    ]
    backlog = group(items, {(0, 1)})
    assert [e["severity"] for e in backlog] == ["critical", "major"]
    assert backlog[0]["reported_by"] == ["chaos", "intruder"] and backlog[0]["duplicates"] == 1
    assert "| QA-" in render(backlog)
