"""Map validated Jev intent decisions to existing project skills."""
from __future__ import annotations

import sqlite3

from ..skills import loader
from .client import JevDecision

MIN_AUTO_ROUTE_CONFIDENCE = 0.95
INTENT_SKILLS = {
    "business_analysis": "ba",
    "summarize_sources": "summarizer",
}


def skill_for_decision(
    conn: sqlite3.Connection,
    project_id: str,
    decision: JevDecision,
    threshold: float = MIN_AUTO_ROUTE_CONFIDENCE,
) -> str | None:
    """Return an enabled fixed skill only for an eligible high-confidence label."""
    skill_id = INTENT_SKILLS.get(decision.category)
    if not skill_id or decision.confidence < threshold:
        return None
    enabled = {
        item["id"]: item["enabled"]
        for item in loader.skills_for_project(conn, project_id)
    }
    if not enabled.get(skill_id, False):
        return None
    return skill_id
