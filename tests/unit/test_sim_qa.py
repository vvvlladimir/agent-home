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


def test_the_lead_judges_pairs_and_verdicts_update_the_backlog(tmp_path):
    import json
    from argparse import Namespace

    from sim.qa.run import apply_verdicts, cmd_coordinate, load_findings

    for role, items in {"chaos": [finding("manager wake returns 500")],
                        "newcomer": [finding("manager wake gives 500 error", severity="minor"),
                                     finding("bad", reproduced_by_me=False)]}.items():  # fmt: skip
        (tmp_path / role).mkdir()
        (tmp_path / role / "findings.json").write_text(json.dumps(items))
    items, rejected = load_findings(tmp_path)
    assert len(items) == 2 and len(rejected) == 1
    cmd_coordinate(Namespace(dir=tmp_path, same="[[0, 1]]", no_model=False, url="", model=""))
    backlog = json.loads((tmp_path / "backlog.json").read_text())["backlog"]
    assert len(backlog) == 1 and backlog[0]["reported_by"] == ["chaos", "newcomer"]
    verdict = tmp_path / "verify" / "QA-001" / "verdict.json"
    assert apply_verdicts(tmp_path)["candidate"] == 1  # no verdict yet: stays a candidate
    verdict.parent.mkdir(parents=True)
    verdict.write_text(json.dumps({"reproduced": True, "evidence": "HTTP 500"}))
    assert apply_verdicts(tmp_path) == {"confirmed": 1, "not_reproduced": 0, "candidate": 0}
    assert "confirmed" in (tmp_path / "backlog.md").read_text()


def test_usefulness_kinds_are_valid_and_every_role_is_complete():
    import yaml

    from sim.qa.run import HERE

    assert validate(finding("x", kind="feature")) == []
    assert validate(finding("x", kind="remove")) == []
    roles = yaml.safe_load((HERE / "roles.yaml").read_text())["roles"]
    assert "hands_off_owner" in roles
    assert all({"traits", "goal", "focus"} <= set(spec) for spec in roles.values())


def test_fussy_tenant_starts_with_one_of_our_automations_to_undo():
    import yaml

    from sim.qa.run import HERE

    seed = yaml.safe_load((HERE / "roles.yaml").read_text())["roles"]["fussy_tenant"]["seed"]
    assert seed and all(yaml.safe_load(draft["yaml"])["actions"] for draft in seed)
