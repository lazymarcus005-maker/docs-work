"""Projects API (ticket #2): create, list, get, update, delete.

Creating a project persists metadata and materializes the physical local
workspace on disk (spec §6.2, §9, FR-001).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..config import Settings
from ..deps import get_app_settings, get_db
from ..storage import filesystem as fs
from ..util import log_event, new_id, now_iso

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectIn(BaseModel):
    name: str
    description: str = ""
    instruction: str = ""


class ProjectPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    instruction: Optional[str] = None
    autonomy_level: Optional[int] = None


AUTONOMY_LEVELS = {
    1: "propose — artifact writes need user approval",
    2: "assisted — agent writes, validation failures escalate",
    3: "autonomous — full auto within project isolation",
}


def get_autonomy_level(conn: sqlite3.Connection, project_id: str) -> int:
    row = conn.execute(
        "SELECT autonomy_level FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    return row["autonomy_level"] if row and row["autonomy_level"] else 2


def get_project(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return dict(row)


def require_project(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    """Project boundary guard used by every project-scoped route."""
    return get_project(conn, project_id)


def _file_counts(conn: sqlite3.Connection, project_id: str) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS files, "
        "SUM(CASE WHEN status = 'READY' THEN 1 ELSE 0 END) AS ready "
        "FROM documents WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    return {"files": row["files"] or 0, "ready": row["ready"] or 0}


@router.get("")
def list_projects(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    rows = conn.execute(
        "SELECT id, name, description, status, created_at, updated_at "
        "FROM projects ORDER BY created_at DESC"
    ).fetchall()
    projects = []
    for r in rows:
        item = dict(r)
        counts = _file_counts(conn, item["id"])
        item["file_count"] = counts["files"]
        item["ready_count"] = counts["ready"]
        projects.append(item)
    return {"projects": projects}


@router.post("", status_code=201)
def create_project(
    body: ProjectIn,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict:
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="Project name is required")

    pid = new_id("prj")
    ts = now_iso()
    conn.execute(
        "INSERT INTO projects (id, name, description, instruction, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?)",
        (pid, body.name.strip(), body.description, body.instruction, ts, ts),
    )
    try:
        fs.ensure_project_dirs(settings.workspace_root, pid)
    except ValueError as e:
        conn.rollback()
        raise HTTPException(status_code=422, detail=str(e))
    (fs.project_dir(settings.workspace_root, pid) / "project.yaml").write_text(
        f"id: {pid}\nname: {body.name.strip()!r}\ncreated: {ts}\n",
        encoding="utf-8",
    )
    conn.commit()
    log_event(conn, "project.created", pid, {"name": body.name.strip()})
    conn.commit()
    return get_project(conn, pid)


@router.get("/{project_id}")
def get_project_route(
    project_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    project = require_project(conn, project_id)
    project["file_count"], project["ready_count"] = (
        _file_counts(conn, project_id)["files"],
        _file_counts(conn, project_id)["ready"],
    )
    return project


@router.patch("/{project_id}")
def update_project(
    project_id: str,
    body: ProjectPatch,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    require_project(conn, project_id)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "name" in fields and not fields["name"].strip():
        raise HTTPException(status_code=422, detail="Project name is required")
    if "autonomy_level" in fields and fields["autonomy_level"] not in AUTONOMY_LEVELS:
        raise HTTPException(
            status_code=422,
            detail=f"autonomy_level must be one of: {sorted(AUTONOMY_LEVELS)}")
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE projects SET {sets}, updated_at = ? WHERE id = ?",
            (*fields.values(), now_iso(), project_id),
        )
        conn.commit()
    return get_project(conn, project_id)


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> None:
    require_project(conn, project_id)
    # tables whose rows belong to a project through project_id directly
    for table in (
        "relations", "entity_aliases", "entities", "documents",
        "processing_jobs", "messages", "agent_runs", "sessions", "skill_runs",
        "artifacts", "project_skills", "conflicts", "review_items",
    ):
        conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,))
    # child tables reached through a parent key
    conn.execute(
        "DELETE FROM relation_evidence WHERE relation_id IN "
        "(SELECT id FROM relations WHERE project_id = ?)",
        (project_id,),
    )
    conn.execute(
        "DELETE FROM relation_evidence WHERE relation_id NOT IN (SELECT id FROM relations)"
    )
    conn.execute(
        "DELETE FROM embeddings WHERE chunk_id IN "
        "(SELECT id FROM chunks WHERE project_id = ?)",
        (project_id,),
    )
    conn.execute(
        "DELETE FROM chunks_fts WHERE chunk_id IN "
        "(SELECT id FROM chunks WHERE project_id = ?)",
        (project_id,),
    )
    conn.execute("DELETE FROM chunks WHERE project_id = ?", (project_id,))
    conn.execute(
        "DELETE FROM document_versions WHERE document_id IN "
        "(SELECT id FROM documents WHERE project_id = ?)",
        (project_id,),
    )
    conn.execute(
        "DELETE FROM artifact_versions WHERE artifact_id IN "
        "(SELECT id FROM artifacts WHERE project_id = ?)",
        (project_id,),
    )
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()
    import shutil

    shutil.rmtree(fs.project_dir(settings.workspace_root, project_id), ignore_errors=True)
    log_event(conn, "project.deleted", project_id, {})
    conn.commit()
