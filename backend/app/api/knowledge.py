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
from ..util import now_iso

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


class ReviewResolveIn(BaseModel):
    action: str  # merge | keep_separate | confirm | dismiss


# ------------------------------------------------- knowledge layer (ticket 12)
@router.get("/entities")
def list_entities(
    project_id: str,
    query: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    require_project(conn, project_id)
    sql = (
        "SELECT e.id, e.type, e.canonical_name, e.meta,"
        " (SELECT COUNT(*) FROM entity_aliases a WHERE a.entity_id = e.id) AS aliases_count,"
        " (SELECT COUNT(*) FROM relations r WHERE r.source_entity_id = e.id"
        "   OR r.target_entity_id = e.id) AS degree"
        " FROM entities e WHERE e.project_id = ?"
    )
    params: list = [project_id]
    if query:
        sql += " AND e.canonical_name LIKE ?"
        params.append(f"%{query}%")
    sql += " ORDER BY degree DESC, e.canonical_name LIMIT 500"
    rows = conn.execute(sql, params).fetchall()
    entities = []
    for r in rows:
        item = dict(r)
        item["aliases"] = [
            a["alias"] for a in conn.execute(
                "SELECT alias FROM entity_aliases WHERE entity_id = ?",
                (item["id"],),
            ).fetchall()
        ]
        entities.append(item)
    return {"entities": entities}


@router.get("/entities/{entity_id}")
def entity_detail(
    project_id: str, entity_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    require_project(conn, project_id)
    row = conn.execute(
        "SELECT * FROM entities WHERE id = ? AND project_id = ?",
        (entity_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    detail = dict(row)
    detail["meta"] = json.loads(detail["meta"] or "{}")
    detail["aliases"] = [a["alias"] for a in conn.execute(
        "SELECT alias FROM entity_aliases WHERE entity_id = ?", (entity_id,)).fetchall()]
    detail["relations"] = []
    for rel in conn.execute(
        "SELECT r.id, r.relation_type, r.confidence,"
        " s.canonical_name AS source, t.canonical_name AS target,"
        " s.id AS source_id, t.id AS target_id"
        " FROM relations r JOIN entities s ON s.id = r.source_entity_id"
        " JOIN entities t ON t.id = r.target_entity_id"
        " WHERE r.source_entity_id = ? OR r.target_entity_id = ?",
        (entity_id, entity_id),
    ).fetchall():
        item = dict(rel)
        item["evidence"] = [
            dict(e) for e in conn.execute(
                "SELECT document_id, chunk_id, page, text FROM relation_evidence"
                " WHERE relation_id = ?", (item["id"],),
            ).fetchall()
        ]
        detail["relations"].append(item)
    return detail


@router.get("/relations")
def list_relations(project_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    require_project(conn, project_id)
    rows = conn.execute(
        "SELECT r.id, r.relation_type, r.confidence, r.status,"
        " s.canonical_name AS source, t.canonical_name AS target"
        " FROM relations r JOIN entities s ON s.id = r.source_entity_id"
        " JOIN entities t ON t.id = r.target_entity_id"
        " WHERE r.project_id = ? ORDER BY r.confidence DESC LIMIT 1000",
        (project_id,),
    ).fetchall()
    relations = []
    for r in rows:
        item = dict(r)
        item["evidence"] = [
            dict(e) for e in conn.execute(
                "SELECT document_id, chunk_id, page, text FROM relation_evidence"
                " WHERE relation_id = ?", (item["id"],),
            ).fetchall()
        ]
        relations.append(item)
    return {"relations": relations}


@router.get("/conflicts")
def list_conflicts(project_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    require_project(conn, project_id)
    rows = conn.execute(
        "SELECT * FROM conflicts WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["details"] = json.loads(item["details"] or "[]")
        out.append(item)
    return {"conflicts": out}


@router.get("/review")
def review_queue(project_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    require_project(conn, project_id)
    rows = conn.execute(
        "SELECT * FROM review_items WHERE project_id = ? AND status = 'open'"
        " ORDER BY created_at",
        (project_id,),
    ).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        item["payload"] = json.loads(item["payload"] or "{}")
        item["suggestion"] = json.loads(item["suggestion"] or "{}")
        items.append(item)
    return {"items": items}


@router.post("/review/{item_id}/resolve")
def resolve_review_item(
    project_id: str,
    item_id: str,
    body: ReviewResolveIn,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    require_project(conn, project_id)
    row = conn.execute(
        "SELECT * FROM review_items WHERE id = ? AND project_id = ? AND status = 'open'",
        (item_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Review item not found")
    payload = json.loads(row["payload"] or "{}")

    from ..knowledge import resolver

    if row["kind"] == "duplicate_entity" and body.action == "merge":
        ids = payload.get("entity_ids", [])
        if len(ids) == 2:
            resolver.merge_entities(conn, project_id, ids[0], ids[1])
    elif row["kind"] == "conflict" and body.action == "resolve_with":
        # user picked a value; record it and close
        conn.execute(
            "UPDATE conflicts SET status = 'resolved' WHERE project_id = ? AND subject = ?",
            (project_id, payload.get("subject", "")),
        )

    conn.execute(
        "UPDATE review_items SET status = 'resolved', resolved_at = ? WHERE id = ?",
        (now_iso(), item_id),
    )
    conn.commit()
    return {"id": item_id, "status": "resolved", "action": body.action}


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
