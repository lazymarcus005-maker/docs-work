"""Jobs + processing status API (ticket #5, spec §28, §37)."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from ..api.projects import require_project
from ..deps import get_db
from ..jobs import queue

router = APIRouter(prefix="/api", tags=["jobs"])


@router.get("/projects/{project_id}/jobs")
def list_project_jobs(
    project_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    require_project(conn, project_id)
    return {"jobs": queue.list_jobs(conn, project_id)}


@router.get("/projects/{project_id}/processing")
def processing_status(
    project_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Per-file processing status for the UI (spec §28)."""
    require_project(conn, project_id)
    files = conn.execute(
        "SELECT id, name, status, error, updated_at FROM documents"
        " WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    jobs = queue.list_jobs(conn, project_id, limit=50)
    active = [j for j in jobs if j["status"] in ("PENDING", "RUNNING")]
    return {
        "files": [dict(f) for f in files],
        "active_jobs": len(active),
        "latest_jobs": jobs[:10],
    }
