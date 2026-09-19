"""Persistent skill plans and task state for the Cowork workspace."""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends

from ..deps import get_db
from .projects import require_project

router = APIRouter(prefix="/api/projects/{project_id}/tasks", tags=["tasks"])


@router.get("")
def list_work_plans(
    project_id: str,
    session_id: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    require_project(conn, project_id)
    query = (
        "SELECT p.*, r.status AS run_status FROM work_plans p"
        " JOIN agent_runs r ON r.id = p.run_id WHERE p.project_id = ?"
    )
    args: list[str] = [project_id]
    if session_id:
        query += " AND p.session_id = ?"
        args.append(session_id)
    query += " ORDER BY p.created_at DESC LIMIT 30"
    rows = conn.execute(query, args).fetchall()
    plans = []
    for row in rows:
        plan = dict(row)
        plan["sources"] = json.loads(plan.pop("sources") or "[]")
        task_rows = conn.execute(
            "SELECT * FROM agent_tasks WHERE plan_id = ? ORDER BY task_order",
            (plan["id"],),
        ).fetchall()
        tasks = []
        for task_row in task_rows:
            task = dict(task_row)
            task["order"] = task.pop("task_order")
            task["source_refs"] = json.loads(task["source_refs"] or "[]")
            task["artifact_refs"] = json.loads(task["artifact_refs"] or "[]")
            tasks.append(task)
        plan["tasks"] = tasks
        plans.append(plan)
    return {"plans": plans}
