"""Which holding tag(s) fund a goal: the one definition for corpus, SIP and the planner's holdings list (the return itself comes from app/planning)."""
from __future__ import annotations


def goal_tags(goal) -> list[str]:
    """The holding goal_tag that funds a goal: its type (retirement is one household goal; whose money counts is the goal's scope, not a second tag)."""
    gt = (goal.goal_type or "").strip()
    return [gt] if gt else []
