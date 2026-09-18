"""Context Manager (ticket #7, spec §18).

Prevents the system from sending an entire project to the LLM: it merges
retrieval results into a ranked, budget-bounded Context Pack. Explicitly
selected files take priority (FR-007).
"""
from __future__ import annotations

import json
import sqlite3

from ..config import Settings
from ..retrieval import text_search


def _document_id_for_name(conn: sqlite3.Connection, project_id: str, name: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM documents WHERE project_id = ? AND (name = ? OR id = ?)",
        (project_id, name, name),
    ).fetchone()
    return row["id"] if row else None


def document_ids_for_files(
    conn: sqlite3.Connection, project_id: str, files: list[str] | None
) -> list[str]:
    """Resolve @file mentions (names or ids) to document ids (FR-007)."""
    return [d for d in (_document_id_for_name(conn, project_id, f) for f in files or []) if d]


def build_context_pack(
    conn: sqlite3.Connection,
    settings: Settings,
    project_id: str,
    task: str,
    selected_files: list[str] | None = None,
    budget_chars: int | None = None,
) -> dict:
    """The spec states the limit in tokens (§18); we budget characters at the
    ~4 chars/token heuristic so it stays comparable across models."""
    budget = budget_chars or settings.context_token_budget * 4
    doc_ids: list[str] = []
    for f in selected_files or []:
        did = _document_id_for_name(conn, project_id, f)
        if did:
            doc_ids.append(did)

    results = text_search.text_search(
        conn, project_id, task, limit=40,
        document_ids=doc_ids or None,
    )

    # rank: keep retrieval order, but selected-file hits first (FR-007)
    if doc_ids:
        results.sort(key=lambda r: 0 if r["document_id"] in doc_ids else 1)

    pack: dict = {
        "task": task,
        "sources": [],
        "entities": [],
        "relations": [],
        "evidence": [],
        "truncated": False,
    }
    used = 0
    included_any = False
    by_doc: dict[str, dict] = {}
    for i, r in enumerate(results):
        chunk_len = len(r["text"]) + 40
        if used + chunk_len > budget:
            if not included_any:
                # always include at least one source, truncated to the budget
                text = r["text"][: max(budget - 40, 200)]
                r = {**r, "text": text + "…"}
                chunk_len = len(r["text"]) + 40
            else:
                pack["truncated"] = True
                break
        used += chunk_len
        included_any = True
        entry = by_doc.setdefault(
            r["document_id"], {"document": r["document_name"], "chunks": [], "texts": []}
        )
        entry["chunks"].append(r["chunk_id"])
        entry["texts"].append({
            "chunk_id": r["chunk_id"],
            "page": r["page"],
            "section_path": r["section_path"],
            "text": r["text"],
        })
        pack["evidence"].append({
            "document_id": r["document_id"],
            "document": r["document_name"],
            "chunk_id": r["chunk_id"],
            "page": r["page"],
            "text": r["text"],
        })
    pack["sources"] = list(by_doc.values())
    pack["used_chars"] = used
    return pack


def render_context_block(pack: dict) -> str:
    """Render the pack as a labeled system-prompt block. Chunk ids are shown
    so the model can cite them; the UI resolves citations back to evidence."""
    if not pack["sources"]:
        return (
            "Project context: no indexed passages matched this task yet. "
            "If project files may contain the answer, use the search tools "
            "before answering; if the project has no relevant context, say so."
        )
    lines = ["Retrieved project context (cite chunk ids you rely on):"]
    for src in pack["sources"]:
        lines.append(f"=== {src['document']} ===")
        for t in src["texts"]:
            location = f"p{t['page']}" if t["page"] else "-"
            section = " > ".join(t["section_path"] or [])
            lines.append(f"[{t['chunk_id']}|{location}|{section}]")
            lines.append(t["text"])
    if pack.get("truncated"):
        lines.append("(context truncated to fit the configured budget)")
    return "\n".join(lines)
