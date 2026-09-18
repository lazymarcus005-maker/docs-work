"""Project search API (ticket #5, spec §36)."""
from __future__ import annotations

import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..api.projects import require_project
from ..deps import get_db
from ..retrieval import text_search

router = APIRouter(prefix="/api/projects/{project_id}/search", tags=["search"])


class SearchIn(BaseModel):
    query: str
    mode: str = "text"  # text | hybrid (hybrid activates with ticket #14)
    limit: int = 20
    document_ids: Optional[List[str]] = None  # @file-mentions narrow the search


@router.post("")
def search_project(
    project_id: str,
    body: SearchIn,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    require_project(conn, project_id)
    limit = max(1, min(body.limit, 100))
    if body.mode == "hybrid":
        from ..retrieval import vector_search

        out = vector_search.hybrid_search(
            conn, project_id, body.query, limit=limit,
            document_ids=body.document_ids,
        )
        return {"query": body.query, "mode": out["mode"], "results": out["results"]}
    results = text_search.text_search(
        conn, project_id, body.query, limit=limit,
        document_ids=body.document_ids,
    )
    return {"query": body.query, "mode": "text", "results": results}
