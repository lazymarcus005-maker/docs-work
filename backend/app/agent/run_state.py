"""Agent run state persistence (spec §19.3)."""
from __future__ import annotations

import json
import sqlite3

from ..util import new_id, now_iso

RUN_STATUSES = ("PENDING", "RUNNING", "WAITING_USER", "SUCCEEDED", "FAILED", "CANCELLED")


def create_run(
    conn: sqlite3.Connection,
    project_id: str,
    session_id: str | None,
    user_message_id: str | None,
    harness_type: str = "native",
    llm_profile_id: str | None = None,
    selected_skill: str | None = None,
) -> str:
    run_id = new_id("run")
    ts = now_iso()
    conn.execute(
        "INSERT INTO agent_runs (id, session_id, project_id, user_message_id,"
        " harness_type, llm_profile_id, selected_skill, status, started_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?)",
        (run_id, session_id, project_id, user_message_id, harness_type,
         llm_profile_id, selected_skill, ts, ts),
    )
    conn.commit()
    return run_id


def update_run(conn: sqlite3.Connection, run_id: str, **fields) -> None:
    allowed = (
        "status", "iteration_count", "tool_call_count", "skill_call_count",
        "error_code", "completed_at", "prompt_tokens", "completion_tokens",
    )
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE agent_runs SET {sets}, updated_at = ? WHERE id = ?",
        (*fields.values(), now_iso(), run_id),
    )
    conn.commit()


def get_run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


_WORKFLOW_TITLES = {
    "understand_task": "Understand the goal",
    "collect_context": "Review project context",
    "analyze_sources": "Analyze source documents",
    "identify_requirements": "Extract requirements",
    "identify_gaps_and_conflicts": "Identify gaps and conflicts",
    "summarize_sources": "Summarize sources",
    "generate_or_patch_artifact": "Draft the artifact",
    "validate_evidence": "Check source evidence",
    "validate_structure": "Validate the document",
    "write_output": "Create the output",
}


def create_skill_plan(
    conn: sqlite3.Connection,
    run_id: str,
    project_id: str,
    session_id: str,
    skill_id: str,
    goal: str,
    sources: list[str],
) -> dict | None:
    """Persist a visible plan from the enabled skill's declared workflow.

    Called only after the harness has loaded project context, so the plan
    never invents steps before knowing which skill and sources are involved.
    """
    from ..skills import loader

    skill = loader.get_skill(conn, skill_id)
    if not skill or not skill.workflow:
        return None
    now = now_iso()
    plan_id = new_id("plan")
    clean_goal = goal.strip()
    conn.execute(
        "INSERT INTO work_plans (id, run_id, project_id, session_id, skill_id,"
        " goal, summary, sources, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (plan_id, run_id, project_id, session_id, skill_id, clean_goal,
         f"{skill.name} workflow for this goal", json.dumps(sources), now, now),
    )
    tasks = []
    current_started = False
    for order, step_id in enumerate(skill.workflow):
        title = _WORKFLOW_TITLES.get(
            step_id, step_id.replace("_", " ").capitalize())
        if step_id in ("understand_task", "collect_context"):
            status = "completed"
            started_at = now
            completed_at = now
        elif not current_started:
            status = "running"
            started_at = now
            completed_at = None
            current_started = True
        else:
            status = "pending"
            started_at = completed_at = None
        task = {
            "id": new_id("task"), "step_id": step_id, "title": title,
            "description": f"{skill.name}: {title.lower()}.",
            "status": status, "order": order, "source_refs": sources,
            "artifact_refs": [], "started_at": started_at,
            "completed_at": completed_at, "reason": None, "error": None,
        }
        conn.execute(
            "INSERT INTO agent_tasks (id, plan_id, step_id, title, description,"
            " status, task_order, source_refs, artifact_refs, started_at,"
            " completed_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task["id"], plan_id, step_id, title, task["description"], status,
             order, json.dumps(sources), "[]", started_at, completed_at, now, now),
        )
        tasks.append(task)
    conn.commit()
    return {
        "id": plan_id, "run_id": run_id, "project_id": project_id,
        "session_id": session_id, "skill_id": skill_id, "goal": clean_goal,
        "summary": f"{skill.name} workflow for this goal", "version": 1,
        "sources": sources, "created_at": now, "updated_at": now,
        "tasks": tasks,
    }


def update_plan_task(
    conn: sqlite3.Connection, plan_id: str, step_id: str, status: str,
    *, error: str | None = None, artifact_ref: str | None = None,
) -> dict | None:
    if status not in ("pending", "running", "completed", "needs_input", "failed", "skipped"):
        raise ValueError(f"Invalid task status: {status}")
    row = conn.execute(
        "SELECT * FROM agent_tasks WHERE plan_id = ? AND step_id = ?",
        (plan_id, step_id),
    ).fetchone()
    if row is None:
        return None
    now = now_iso()
    refs = json.loads(row["artifact_refs"] or "[]")
    if artifact_ref and artifact_ref not in refs:
        refs.append(artifact_ref)
    started_at = row["started_at"] or (now if status == "running" else None)
    completed_at = now if status in ("completed", "failed", "skipped") else None
    conn.execute(
        "UPDATE agent_tasks SET status = ?, started_at = ?, completed_at = ?,"
        " error = ?, artifact_refs = ?, updated_at = ? WHERE id = ?",
        (status, started_at, completed_at, error, json.dumps(refs), now, row["id"]),
    )
    conn.execute("UPDATE work_plans SET updated_at = ? WHERE id = ?", (now, plan_id))
    conn.commit()
    updated = dict(row)
    updated.update(status=status, started_at=started_at, completed_at=completed_at,
                   error=error, artifact_refs=json.dumps(refs), updated_at=now)
    updated["source_refs"] = json.loads(updated["source_refs"] or "[]")
    updated["artifact_refs"] = refs
    updated["order"] = updated.pop("task_order")
    updated["plan_id"] = plan_id
    return updated


def activate_plan_step(conn: sqlite3.Connection, plan_id: str, step_id: str) -> list[dict]:
    target = conn.execute(
        "SELECT task_order FROM agent_tasks WHERE plan_id = ? AND step_id = ?",
        (plan_id, step_id),
    ).fetchone()
    if target is None:
        return []
    before = conn.execute(
        "SELECT step_id FROM agent_tasks WHERE plan_id = ? AND status = 'running'"
        " AND step_id != ? ORDER BY task_order",
        (plan_id, step_id),
    ).fetchall()
    updates = []
    for row in before:
        updates.append(update_plan_task(conn, plan_id, row["step_id"], "completed"))
    current = update_plan_task(conn, plan_id, step_id, "running")
    if current:
        updates.append(current)
    return [item for item in updates if item]


def complete_plan_step(conn: sqlite3.Connection, plan_id: str, step_id: str,
                       *, error: str | None = None,
                       artifact_ref: str | None = None) -> list[dict]:
    updates = []
    current = update_plan_task(
        conn, plan_id, step_id, "failed" if error else "completed",
        error=error, artifact_ref=artifact_ref,
    )
    if current:
        updates.append(current)
    return updates


def finish_skill_plan(conn: sqlite3.Connection, plan_id: str, succeeded: bool,
                      cancelled: bool = False) -> None:
    current = conn.execute(
        "SELECT step_id FROM agent_tasks WHERE plan_id = ? AND status = 'running'"
        " ORDER BY task_order LIMIT 1", (plan_id,),
    ).fetchone()
    if cancelled and current:
        update_plan_task(conn, plan_id, current["step_id"], "pending")
        return
    if not succeeded and current:
        update_plan_task(conn, plan_id, current["step_id"], "failed",
                         error="Skill run failed")
        return
    pending = conn.execute(
        "SELECT step_id FROM agent_tasks WHERE plan_id = ?"
        " AND status IN ('pending', 'running') ORDER BY task_order", (plan_id,),
    ).fetchall()
    for row in pending:
        update_plan_task(conn, plan_id, row["step_id"], "completed")


def resume_waiting_plan(conn: sqlite3.Connection, project_id: str, session_id: str,
                        run_id: str) -> dict | None:
    """Continue the most recent plan paused for user input in this session."""
    row = conn.execute(
        "SELECT p.id FROM work_plans p JOIN agent_runs r ON r.id = p.run_id"
        " JOIN agent_tasks t ON t.plan_id = p.id"
        " WHERE p.project_id = ? AND p.session_id = ? AND r.status = 'WAITING_USER'"
        " AND t.status = 'needs_input' ORDER BY p.updated_at DESC LIMIT 1",
        (project_id, session_id),
    ).fetchone()
    if row is None:
        return None
    plan_id = row["id"]
    conn.execute(
        "UPDATE work_plans SET run_id = ?, updated_at = ? WHERE id = ?",
        (run_id, now_iso(), plan_id),
    )
    task = conn.execute(
        "SELECT step_id FROM agent_tasks WHERE plan_id = ? AND status = 'needs_input'"
        " ORDER BY task_order DESC LIMIT 1", (plan_id,),
    ).fetchone()
    if task:
        update_plan_task(conn, plan_id, task["step_id"], "running")
    return get_plan(conn, plan_id)


def get_plan(conn: sqlite3.Connection, plan_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM work_plans WHERE id = ?", (plan_id,)).fetchone()
    if not row:
        return None
    plan = dict(row)
    plan["sources"] = json.loads(plan["sources"] or "[]")
    tasks = []
    for task_row in conn.execute(
        "SELECT * FROM agent_tasks WHERE plan_id = ? ORDER BY task_order", (plan_id,),
    ).fetchall():
        task = dict(task_row)
        task["order"] = task.pop("task_order")
        task["source_refs"] = json.loads(task["source_refs"] or "[]")
        task["artifact_refs"] = json.loads(task["artifact_refs"] or "[]")
        tasks.append(task)
    plan["tasks"] = tasks
    return plan
