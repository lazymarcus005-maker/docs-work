"""Vector search + hybrid ranking (ticket #14, spec §17)."""
from __future__ import annotations

import json
import sqlite3
import struct

from ..embeddings import provider as emb_provider
from .text_search import text_search

BATCH = 500  # stream embeddings in batches — never load everything at once


def pack_vector(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_vector(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def embed_document_chunks(
    conn: sqlite3.Connection, provider: emb_provider.EmbeddingProvider, document_id: str
) -> int:
    """Embed chunks that lack a current-model vector (cache semantics, §33)."""
    pending = conn.execute(
        "SELECT c.id, c.text FROM chunks c LEFT JOIN embeddings e"
        " ON e.chunk_id = c.id AND e.model = ?"
        " WHERE c.document_id = ? AND e.chunk_id IS NULL",
        (provider.model, document_id),
    ).fetchall()
    if not pending:
        return 0
    # model switch invalidates cached vectors of the old model (§33)
    conn.execute("DELETE FROM embeddings WHERE model != ?", (provider.model,))
    from ..util import now_iso

    ts = now_iso()
    vectors = provider.embed([c["text"] for c in pending])
    for chunk_row, vec in zip(pending, vectors):
        conn.execute(
            "INSERT OR REPLACE INTO embeddings (chunk_id, model, dim, vector, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (chunk_row["id"], provider.model, len(vec), pack_vector(vec), ts),
        )
    conn.commit()
    return len(pending)


def vector_search(
    conn: sqlite3.Connection,
    project_id: str,
    query: str,
    provider: emb_provider.EmbeddingProvider,
    limit: int = 20,
) -> list[dict]:
    query_vec = provider.embed([query])[0]
    scored: list[tuple[float, dict]] = []
    cursor = conn.execute(
        "SELECT e.chunk_id, e.vector, c.document_id, d.name AS document_name,"
        " c.section_path, c.text, c.page FROM embeddings e"
        " JOIN chunks c ON c.id = e.chunk_id"
        " JOIN documents d ON d.id = c.document_id"
        " WHERE e.model = ? AND c.project_id = ?",
        (provider.model, project_id),
    )
    while True:
        rows = cursor.fetchmany(BATCH)
        if not rows:
            break
        for r in rows:
            score = emb_provider.cosine(query_vec, unpack_vector(r["vector"]))
            item = dict(r)
            item["vector_score"] = score
            scored.append((score, item))
    scored.sort(key=lambda t: -t[0])
    results = []
    for score, item in scored[:limit]:
        item["section_path"] = json.loads(item["section_path"] or "[]")
        item["score"] = score
        results.append(item)
    return results


def hybrid_search(
    conn: sqlite3.Connection,
    project_id: str,
    query: str,
    limit: int = 20,
    provider: emb_provider.EmbeddingProvider | None = None,
    document_ids: list[str] | None = None,
) -> dict:
    """Merge text + vector results into one ranked list (spec §17)."""
    text_results = text_search(conn, project_id, query, limit=limit,
                               document_ids=document_ids)
    provider = provider or emb_provider.get_provider()
    try:
        vector_results = vector_search(conn, project_id, query, provider, limit=limit)
        if document_ids:
            allowed = set(document_ids)
            vector_results = [r for r in vector_results
                              if r["document_id"] in allowed]
    except Exception:  # noqa: BLE001 — vector side is an enhancement
        vector_results = []

    if not vector_results:
        return {"mode": "text", "results": text_results}

    # weighted reciprocal-rank fusion
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}
    for rank, r in enumerate(text_results):
        scores[r["chunk_id"]] = scores.get(r["chunk_id"], 0.0) + 1.0 / (rank + 1)
        items[r["chunk_id"]] = r
    for rank, r in enumerate(vector_results):
        scores[r["chunk_id"]] = scores.get(r["chunk_id"], 0.0) + 1.0 / (rank + 1)
        items.setdefault(r["chunk_id"], r)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
    results = []
    for chunk_id, score in ranked:
        item = items[chunk_id]
        item["score"] = score
        results.append(item)
    return {"mode": "hybrid", "results": results}
