"""Agent run state persistence (spec §19.3)."""
from __future__ import annotations

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
        "error_code", "completed_at",
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
