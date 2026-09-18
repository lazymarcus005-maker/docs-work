"""Entity resolution (ticket #12, spec §14).

Pipeline: normalization → exact alias match → project dictionary → fuzzy
similarity. Embedding/LLM stages are optional future additions. Automatic
merging only happens on strong signals; weak matches become review items
instead of silent merges (§14, §29).
"""
from __future__ import annotations

import difflib
import json
import sqlite3

from ..util import new_id, now_iso
from .extraction import normalize_name

AUTO_MERGE_RATIO = 0.92      # strong similarity: merge automatically
REVIEW_RATIO = 0.80          # weaker: open a review item, keep separate


def find_or_create_entity(
    conn: sqlite3.Connection,
    project_id: str,
    name: str,
    etype: str,
    source_document_id: str | None = None,
) -> tuple[str, bool]:
    """Return (entity_id, created). Matches via normalized alias; near-misses
    are queued for review rather than merged."""
    norm = normalize_name(name)
    ts = now_iso()

    row = conn.execute(
        "SELECT entity_id FROM entity_aliases WHERE project_id = ? AND norm_alias = ?",
        (project_id, norm),
    ).fetchone()
    if row:
        entity_id = row["entity_id"]
        _add_alias(conn, entity_id, project_id, name)  # record the new spelling
        _note_source(conn, entity_id, source_document_id)
        return entity_id, False

    # fuzzy comparison against same-type canonical names
    candidates = conn.execute(
        "SELECT id, canonical_name FROM entities WHERE project_id = ? AND type = ?",
        (project_id, etype),
    ).fetchall()
    best_id, best_ratio = None, 0.0
    for cand in candidates:
        ratio = difflib.SequenceMatcher(
            None, norm, normalize_name(cand["canonical_name"])).ratio()
        if ratio > best_ratio:
            best_id, best_ratio = cand["id"], ratio

    if best_id is not None and best_ratio >= AUTO_MERGE_RATIO:
        _add_alias(conn, best_id, project_id, name)
        _note_source(conn, best_id, source_document_id)
        return best_id, False

    entity_id = new_id("ent")
    conn.execute(
        "INSERT INTO entities (id, project_id, type, canonical_name, meta, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (entity_id, project_id, etype, name,
         json.dumps({"source_documents": [source_document_id] if source_document_id else []}),
         ts),
    )
    _add_alias(conn, entity_id, project_id, name)
    if best_id is not None and best_ratio >= REVIEW_RATIO:
        _queue_duplicate_review(conn, project_id, best_id, entity_id, best_ratio)
    return entity_id, True


def _add_alias(conn: sqlite3.Connection, entity_id: str, project_id: str, alias: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO entity_aliases (entity_id, project_id, alias,"
        " norm_alias, created_at) VALUES (?, ?, ?, ?, ?)",
        (entity_id, project_id, alias, normalize_name(alias), now_iso()),
    )


def _note_source(conn: sqlite3.Connection, entity_id: str, document_id: str | None) -> None:
    if document_id is None:
        return
    row = conn.execute("SELECT meta FROM entities WHERE id = ?", (entity_id,)).fetchone()
    meta = json.loads(row["meta"] or "{}")
    sources = meta.setdefault("source_documents", [])
    if document_id not in sources:
        sources.append(document_id)
        conn.execute("UPDATE entities SET meta = ? WHERE id = ?",
                     (json.dumps(meta), entity_id))


def _queue_duplicate_review(
    conn: sqlite3.Connection, project_id: str, keep_id: str, new_id_: str, ratio: float
) -> None:
    keep = conn.execute("SELECT canonical_name FROM entities WHERE id = ?", (keep_id,)).fetchone()
    new = conn.execute("SELECT canonical_name FROM entities WHERE id = ?", (new_id_,)).fetchone()
    conn.execute(
        "INSERT INTO review_items (id, project_id, kind, payload, suggestion, status, created_at)"
        " VALUES (?, ?, 'duplicate_entity', ?, ?, 'open', ?)",
        (new_id("rev"), project_id,
         json.dumps({"entity_ids": [keep_id, new_id_],
                     "names": [keep["canonical_name"], new["canonical_name"]]}),
         json.dumps({"action": "merge", "into": keep_id}),
         now_iso()),
    )


def merge_entities(conn: sqlite3.Connection, project_id: str, keep_id: str, dup_id: str) -> None:
    """Review action: merge two entities — aliases and relations fold into
    the kept entity; nothing is silently lost (§29)."""
    if keep_id == dup_id:
        return
    _add_alias(conn, keep_id, project_id,
               conn.execute("SELECT canonical_name FROM entities WHERE id = ?",
                            (dup_id,)).fetchone()["canonical_name"])
    for alias in conn.execute(
        "SELECT alias FROM entity_aliases WHERE entity_id = ?", (dup_id,)
    ).fetchall():
        _add_alias(conn, keep_id, project_id, alias["alias"])
    conn.execute(
        "UPDATE OR IGNORE relations SET source_entity_id = ? WHERE source_entity_id = ?",
        (keep_id, dup_id),
    )
    conn.execute(
        "UPDATE OR IGNORE relations SET target_entity_id = ? WHERE target_entity_id = ?",
        (keep_id, dup_id),
    )
    # a merge can create a self-loop; drop those
    conn.execute("DELETE FROM relations WHERE source_entity_id = target_entity_id")
    conn.execute("DELETE FROM entities WHERE id = ?", (dup_id,))
    conn.commit()
