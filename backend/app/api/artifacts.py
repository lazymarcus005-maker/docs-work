"""Artifacts API (ticket #8, spec §23): list, versions, read, edit
(user edits create new versions), download, delete."""
from __future__ import annotations

import sqlite3

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..artifacts import manager
from ..config import Settings
from ..deps import get_app_settings, get_db
from ..storage import filesystem as fs
from ..util import now_iso
from .projects import require_project

router = APIRouter(prefix="/api/projects/{project_id}/artifacts", tags=["artifacts"])


class ArtifactEdit(BaseModel):
    content: str


def _artifact(conn, project_id, artifact_id):
    try:
        return manager.get_artifact(conn, project_id, artifact_id)
    except manager.ArtifactError:
        raise HTTPException(status_code=404, detail="Artifact not found")


@router.get("")
def list_artifacts(project_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    require_project(conn, project_id)
    return {"artifacts": manager.list_artifacts(conn, project_id)}


@router.get("/{artifact_id}")
def get_artifact(
    project_id: str, artifact_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    require_project(conn, project_id)
    return _artifact(conn, project_id, artifact_id)


@router.post("/{artifact_id}/approve")
def approve_artifact(
    project_id: str,
    artifact_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Autonomy L1: user approves a proposed artifact (spec §19 gate)."""
    require_project(conn, project_id)
    artifact = _artifact(conn, project_id, artifact_id)
    if artifact["validation_status"] != "proposed":
        raise HTTPException(status_code=409,
                            detail="Artifact is not awaiting approval")
    conn.execute(
        "UPDATE artifacts SET validation_status = 'unverified', updated_at = ?"
        " WHERE id = ?", (__import__("app.util", fromlist=["now_iso"]).now_iso(), artifact_id))
    conn.execute(
        "UPDATE review_items SET status = 'resolved', resolved_at = ?"
        " WHERE project_id = ? AND kind = 'artifact_approval'"
        " AND status = 'open' AND payload LIKE ?",
        (__import__("app.util", fromlist=["now_iso"]).now_iso(), project_id,
         f"%{artifact_id}%"),
    )
    conn.commit()
    return _artifact(conn, project_id, artifact_id)


@router.get("/{artifact_id}/content")
def artifact_content(
    project_id: str,
    artifact_id: str,
    version: Optional[int] = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    require_project(conn, project_id)
    _artifact(conn, project_id, artifact_id)
    try:
        return manager.read_version(conn, project_id, artifact_id, version)
    except manager.ArtifactError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/{artifact_id}")
def edit_artifact(
    project_id: str,
    artifact_id: str,
    body: ArtifactEdit,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict:
    """User edit in the artifact editor — preserved as a new version."""
    require_project(conn, project_id)
    artifact = _artifact(conn, project_id, artifact_id)
    return manager.write_artifact(
        conn, settings, project_id,
        file_name=artifact["file_name"], content=body.content,
        created_by="user",
    )


@router.get("/{artifact_id}/download")
def download_artifact(
    project_id: str,
    artifact_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> FileResponse:
    require_project(conn, project_id)
    artifact = _artifact(conn, project_id, artifact_id)
    path = fs.safe_join(
        fs.outputs_dir(settings.workspace_root, project_id), artifact["file_name"]
    )
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")
    return FileResponse(path, filename=artifact["file_name"])


@router.delete("/{artifact_id}", status_code=204)
def delete_artifact(
    project_id: str,
    artifact_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> None:
    require_project(conn, project_id)
    _artifact(conn, project_id, artifact_id)
    manager.delete_artifact(conn, settings, project_id, artifact_id)
