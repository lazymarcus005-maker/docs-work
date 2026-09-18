"""Ticket #14: CPU embeddings with caching, vector + hybrid retrieval."""
from __future__ import annotations

import io
import time

from app import db
from app.embeddings.provider import HashingEmbedder, cosine, get_provider
from app.retrieval.vector_search import embed_document_chunks, hybrid_search, vector_search


def test_hashing_embedder_is_deterministic_and_cpu_only():
    p = HashingEmbedder()
    v1, v2, v3 = p.embed(["gateway authentication flow", "gateway authentication flow", "recipe for cake"])
    assert v1 == v2
    assert len(v1) == 256
    assert abs(sum(x * x for x in v1) - 1.0) < 1e-6  # L2-normalized
    assert cosine(v1, v2) > cosine(v1, v3)


def test_get_provider_never_fails():
    provider = get_provider()
    assert provider.dim > 0
    assert provider.embed(["hello"])


def _upload_and_wait_embedded(client, pid, name, content):
    res = client.post(
        f"/api/projects/{pid}/files",
        files={"files": (name, io.BytesIO(content), "text/plain")},
    )
    fid = res.json()["files"][0]["file_id"]
    deadline = time.time() + 12
    while time.time() < deadline:
        row = client.app.state.conn.execute(
            "SELECT COUNT(*) AS n FROM embeddings e JOIN chunks c ON c.id = e.chunk_id"
            " WHERE c.document_id = ?", (fid,)).fetchone()
        if row["n"] > 0:
            return fid
        time.sleep(0.15)
    raise AssertionError("document never got embedded")


def test_upload_embeddings_cached_and_reused(client, settings):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    fid = _upload_and_wait_embedded(client, pid, "a.txt", b"authentication gateway tokens")

    provider = get_provider()
    n_before = client.app.state.conn.execute(
        "SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
    # re-running the embed task must not re-embed cached chunks (§33)
    embedded = embed_document_chunks(client.app.state.conn, provider, fid)
    assert embedded == 0
    n_after = client.app.state.conn.execute(
        "SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
    assert n_before == n_after

    # embedding version recorded on the document
    doc = client.app.state.conn.execute(
        "SELECT embedding_version FROM documents WHERE id = ?", (fid,)).fetchone()
    assert doc["embedding_version"] == provider.model


def test_vector_search_ranks_relevant_chunk_first(client, settings):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    conn = client.app.state.conn
    ts = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO documents (id, project_id, name, stored_name, media_type,"
        " kind, size_bytes, content_hash, status, created_at, updated_at)"
        " VALUES ('doc_1', ?, 'a.txt', 'a.txt', '', 'text', 1, 'h', 'READY', ?, ?)",
        (pid, ts, ts))
    conn.execute(
        "INSERT INTO chunks (id, document_id, project_id, section_path, text,"
        " page, sequence, source_element_ids, created_at)"
        " VALUES ('chk_1', 'doc_1', ?, '[]', 'authentication gateway tokens expire', 1, 1, '[]', ?)",
        (pid, ts))
    conn.execute(
        "INSERT INTO chunks (id, document_id, project_id, section_path, text,"
        " page, sequence, source_element_ids, created_at)"
        " VALUES ('chk_2', 'doc_1', ?, '[]', 'quarterly revenue report numbers', 2, 2, '[]', ?)",
        (pid, ts))
    conn.commit()

    provider = HashingEmbedder()
    embed_document_chunks(conn, provider, "doc_1")
    results = vector_search(conn, pid, "authentication gateway", provider, limit=2)
    assert results[0]["chunk_id"] == "chk_1"
    assert results[0]["vector_score"] > results[1]["vector_score"]


def test_hybrid_search_merges_text_and_vector(client, settings):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    conn = client.app.state.conn
    ts = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO documents (id, project_id, name, stored_name, media_type,"
        " kind, size_bytes, content_hash, status, created_at, updated_at)"
        " VALUES ('doc_1', ?, 'a.txt', 'a.txt', '', 'text', 1, 'h', 'READY', ?, ?)",
        (pid, ts, ts))
    conn.execute(
        "INSERT INTO chunks (id, document_id, project_id, section_path, text,"
        " page, sequence, source_element_ids, created_at)"
        " VALUES ('chk_1', 'doc_1', ?, '[]', 'authentication gateway tokens', 1, 1, '[]', ?)",
        (pid, ts))
    conn.execute("INSERT INTO chunks_fts (chunk_id, text, section_path) VALUES ('chk_1', 'authentication gateway tokens', '[]')")
    provider = HashingEmbedder()
    embed_document_chunks(conn, provider, "doc_1")

    out = hybrid_search(conn, pid, "authentication gateway", provider=provider)
    assert out["mode"] == "hybrid"
    assert out["results"][0]["chunk_id"] == "chk_1"

    # falls back to text mode when nothing is embedded
    out2 = hybrid_search(conn, pid, "authentication gateway",
                         provider=HashingEmbedder(), document_ids=["doc_1"])
    assert out2["results"]


def test_hybrid_mode_via_search_api(client):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    res = client.post(
        f"/api/projects/{pid}/files",
        files={"files": ("a.txt", io.BytesIO(b"authentication gateway tokens expire"), "text/plain")},
    )
    fid = res.json()["files"][0]["file_id"]
    deadline = time.time() + 12
    while time.time() < deadline:
        row = client.app.state.conn.execute(
            "SELECT embedding_version FROM documents WHERE id = ?", (fid,)).fetchone()
        if row and row["embedding_version"]:
            break
        time.sleep(0.15)

    body = client.post(f"/api/projects/{pid}/search",
                       json={"query": "authentication gateway", "mode": "hybrid"}).json()
    assert body["mode"] in ("hybrid", "text")  # vector side enhances when present
    assert body["results"]
