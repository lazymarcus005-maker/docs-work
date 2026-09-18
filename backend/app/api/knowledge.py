"""Knowledge API (tickets #10/#12/#13): evidence resolution now; entities,
relations, and review queue land with the knowledge layer."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..api.projects import require_project
from ..deps import get_db

router = APIRouter(prefix="/api/projects/{project_id}", tags=["knowledge"])


def _chunk_evidence(conn: sqlite3.Connection, project_id: str, chunk_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT c.id AS chunk_id, c.page, c.section_path, c.text, d.name AS document,"
        " d.id AS document_id FROM chunks c JOIN documents d ON d.id = c.document_id"
        " WHERE c.id = ? AND c.project_id = ?",
        (chunk_id, project_id),
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["section_path"] = json.loads(item["section_path"] or "[]")
    return item


@router.get("/evidence/{chunk_id}")
def chunk_evidence(
    project_id: str, chunk_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Open the source behind any citation (spec §15, Scenario F)."""
    require_project(conn, project_id)
    evidence = _chunk_evidence(conn, project_id, chunk_id)
    if evidence is None:
        raise HTTPException(status_code=404, detail=f"No evidence for {chunk_id}")
    return evidence


class ResolveIn(BaseModel):
    chunk_ids: List[str]


@router.post("/evidence/resolve")
def resolve_evidence(
    project_id: str, body: ResolveIn, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Resolve the chunk ids cited by an answer or artifact to source
    locations; unknown ids are reported rather than dropped silently."""
    require_project(conn, project_id)
    resolved, missing = [], []
    for cid in body.chunk_ids:
        evidence = _chunk_evidence(conn, project_id, cid)
        (resolved if evidence else missing).append(evidence or cid)
    return {"evidence": resolved, "missing": missing}


@router.get("/artifacts/{artifact_id}/evidence")
def artifact_evidence(
    project_id: str, artifact_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Machine-readable citations of a generated artifact → source locations
    (§24, FR-011, Scenario F)."""
    require_project(conn, project_id)
    row = conn.execute(
        "SELECT id FROM artifacts WHERE id = ? AND project_id = ?",
        (artifact_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Artifact not found")

    from ..artifacts import manager

    version = manager.read_version(conn, project_id, artifact_id)
    cited = sorted(set(re.findall(r"\bchk_[A-Za-z0-9_]+\b", version["content"])))
    for entry in json.loads(version["source_context"] or "[]"):
        cid = entry.get("chunk_id") if isinstance(entry, dict) else entry
        if cid and cid not in cited:
            cited.append(cid)

    resolved, missing = [], []
    for cid in cited:
        evidence = _chunk_evidence(conn, project_id, cid)
        (resolved if evidence else missing).append(evidence or {"chunk_id": cid})
    return {"citations": resolved, "unresolved": missing}
