"""What an agent's message to the owner claims, checked against what actually happened (no model).

QA-007 and the stories of 2026-11-15: in shadow mode agents told the owner that a heater "awaits
approval" (nothing waited: shadow records, it asks nobody) and that they "turned off the hallway
light" (it was only recorded). A report that claims either, while the run's actions say otherwise,
goes back to the model to be rewritten instead of reaching the inbox. `sim/invariants.py` checks the
whole trace with the same patterns.
"""

from __future__ import annotations

import re

# a claim that something now waits for the owner ("was sent for owner approval", "awaiting your
# approval"), not a rule ("T2 needs owner approval") and not a denial ("none is waiting for approval")
_WHOSE = r"(?:your |owner |the owner'?s? )?"
APPROVAL = re.compile(
    r"\b(?:sent|submitted|queued|forwarded|passed) (?:to you |to the owner )?for " + _WHOSE + r"approv"
    r"|\b(?:is |are |now )?(?:waiting|awaiting|pending) (?:for )?" + _WHOSE + r"approv",
    re.I,
)
DENIAL = re.compile(r"\b(?:none|nothing|no \w+|not|neither|nor|never)\b[^.]{0,30}$", re.I)
DONE_CLAIM = re.compile(
    r"\b(?:I (?:have |'ve )?(?:turned|switched|closed|opened|locked|unlocked|stopped)"
    r"|(?:was|has been|have been) (?:turned|switched|closed|opened|locked|unlocked|stopped))\b",
    re.I,
)


def claims_approval(text: str) -> bool:
    return any(not DENIAL.search(text[: m.start()]) for m in APPROVAL.finditer(text))


def claims_done(text: str) -> bool:
    return any(not DENIAL.search(text[: m.start()]) for m in DONE_CLAIM.finditer(text))


def false_claim(text: str, verdicts: list[str]) -> str | None:
    """Why `text` tells the owner something the run's actions (their verdicts) do not back, or None.
    A change is only claimed falsely when the run recorded actions and none was executed: "the
    lamp was turned off" may describe what a person did."""
    if claims_approval(text) and "pending" not in verdicts:
        return (
            "it says something waits for the owner's approval, but nothing does (shadow mode records "
            "actions, it asks nobody)"
        )
    if verdicts and claims_done(text) and not {"executed", "approved"} & set(verdicts):
        return "it says something was changed, but this run's actions were only recorded (shadow mode)"
    return None
