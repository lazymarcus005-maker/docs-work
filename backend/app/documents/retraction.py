"""Retraction: what happens to derived data when a source is deleted
(ticket #11, spec §60). Also used by basic file deletion (ticket #3).

Deleting a source removes: the stored file, its parsed artifacts, its chunks,
embeddings, FTS rows, processing jobs, and knowledge assertions supported
only by that source. Assertions supported by other documents remain.
"""
from __future__ import annotations

import json
import sqlite3

from ..config import Settings
from ..storage import filesystem as fs


def purge_document(
    conn: sqlite3.Connection, settings: Settings, project_id: str, doc: dict
) -> dict:
    doc_id = doc["id"]

    # 1. stored source file + parsed artifact
    (fs.context_dir(settings.workspace_root, project_id) / doc["stored_name"]).unlink(
        missing_ok=True
    )
    (fs.parsed_dir(settings.workspace_root, project_id) / f"{doc_id}.json").unlink(
        missing_ok=True
    )

    # 2. chunks + their embeddings + FTS rows
    chunk_ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM chunks WHERE document_id = ?", (doc_id,)
        ).fetchall()
    ]
    for cid in chunk_ids:
        conn.execute("DELETE FROM embeddings WHERE chunk_id = ?", (cid,))
        conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (cid,))
    conn.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))

    # 3. relation evidence from this document; relations left unsupported die
    conn.execute(
        "DELETE FROM relation_evidence WHERE document_id = ?", (doc_id,)
    )
    conn.execute(
        "DELETE FROM relations WHERE id NOT IN "
        "(SELECT DISTINCT relation_id FROM relation_evidence)"
    )
    _drop_orphan_entities(conn, project_id, doc_id)

    # 4. jobs, versions, document row
    conn.execute("DELETE FROM processing_jobs WHERE document_id = ?", (doc_id,))
    conn.execute("DELETE FROM document_versions WHERE document_id = ?", (doc_id,))
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    conn.commit()

    return {"removed_chunks": len(chunk_ids)}


def _drop_orphan_entities(
    conn: sqlite3.Connection, project_id: str, removed_doc_id: str
) -> None:
    """Entities extracted only from the removed document and no longer
    participating in any relation are removed (spec §60)."""
    for row in conn.execute(
        "SELECT id, meta FROM entities WHERE project_id = ?", (project_id,)
    ).fetchall():
        meta = json.loads(row["meta"] or "{}")
        sources = set(meta.get("source_documents", []))
        if sources - {removed_doc_id}:
            continue  # supported by other documents — stays (spec §60)
        in_relation = conn.execute(
            "SELECT 1 FROM relations WHERE source_entity_id = ? "
            "OR target_entity_id = ? LIMIT 1",
            (row["id"], row["id"]),
        ).fetchone()
        has_other_alias = conn.execute(
            "SELECT 1 FROM relation_evidence re JOIN chunks c "
            "ON c.id = re.chunk_id WHERE (re.relation_id IN "
            "(SELECT id FROM relations WHERE source_entity_id = ? "
            "OR target_entity_id = ?)) LIMIT 1",
            (row["id"], row["id"]),
        ).fetchone()
        if not in_relation and not has_other_alias:
            conn.execute("DELETE FROM entities WHERE id = ?", (row["id"],))
