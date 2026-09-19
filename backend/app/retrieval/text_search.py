"""Text search over the project's chunk index (ticket #5, spec §17, FR-005)."""
from __future__ import annotations

import json
import re
import sqlite3


def _fts_query(query: str) -> str:
    """Build a tolerant prefix query, treating punctuation as a word boundary."""
    terms = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
    return " ".join(f'"{term}"*' for term in terms)


def text_search(
    conn: sqlite3.Connection,
    project_id: str,
    query: str,
    limit: int = 20,
    document_ids: list[str] | None = None,
) -> list[dict]:
    fts = _fts_query(query)
    if not fts:
        return []
    sql = (
        "SELECT c.id AS chunk_id, c.document_id, d.name AS document_name,"
        " c.section_path, c.text, c.page, bm25(chunks_fts) AS score"
        " FROM chunks_fts f JOIN chunks c ON c.id = f.chunk_id"
        " JOIN documents d ON d.id = c.document_id"
        " WHERE chunks_fts MATCH ? AND c.project_id = ?"
    )
    params: list = [fts, project_id]
    if document_ids:
        marks = ",".join("?" for _ in document_ids)
        sql += f" AND c.document_id IN ({marks})"
        params.extend(document_ids)
    sql += " ORDER BY score LIMIT ?"
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
        if not rows and " " in fts:
            # AND across terms missed (e.g. compound tokens); widen to OR
            or_query = " OR ".join(fts.split())
            rows = conn.execute(sql, [or_query, *params[1:]]).fetchall()
    except sqlite3.OperationalError:
        # malformed FTS query — degrade to LIKE substring search
        like = f"%{query.strip()}%"
        sql = (
            "SELECT c.id AS chunk_id, c.document_id, d.name AS document_name,"
            " c.section_path, c.text, c.page, -1000.0 AS score"
            " FROM chunks c JOIN documents d ON d.id = c.document_id"
            " WHERE c.project_id = ? AND c.text LIKE ?"
        )
        params = [project_id, like]
        if document_ids:
            marks = ",".join("?" for _ in document_ids)
            sql += f" AND c.document_id IN ({marks})"
            params.extend(document_ids)
        sql += " LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()

    results = []
    for r in rows:
        item = dict(r)
        item["section_path"] = json.loads(item["section_path"] or "[]")
        results.append(item)
    return results
