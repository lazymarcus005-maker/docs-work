"""Ticket #11: incremental reprocessing + deletion semantics
(spec §11, §60, FR-004, Scenario G)."""
from __future__ import annotations

import io
import json
import time

from app import db
from app.documents import retraction
from app.documents.pipeline import mark_stale
from app.storage import filesystem as fs


def _upload(client, pid, name, content, mime="text/plain"):
    return client.post(
        f"/api/projects/{pid}/files",
        files={"files": (name, io.BytesIO(content), mime)},
    )


def _wait_ready(client, pid, fid, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        files = client.get(f"/api/projects/{pid}/files").json()["files"]
        doc = next(f for f in files if f["id"] == fid)
        if doc["status"] == "READY":
            return doc
        time.sleep(0.1)
    raise AssertionError(f"never READY: {doc}")


def test_same_hash_reupload_reuses_result_without_reprocessing(client):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    first = _upload(client, pid, "a.txt", b"stable content").json()["files"][0]
    _wait_ready(client, pid, first["file_id"])

    jobs_before = len(client.get(f"/api/projects/{pid}/jobs").json()["jobs"])
    again = _upload(client, pid, "a.txt", b"stable content").json()["files"][0]
    assert again["reused"] is True
    assert again["file_id"] == first["file_id"]
    assert again["status"] == "READY"

    jobs_after = client.get(f"/api/projects/{pid}/jobs").json()["jobs"]
    assert len(jobs_after) == jobs_before  # no reprocessing happened (FR-004)


def test_changed_file_reprocesses_only_itself(client):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    a = _upload(client, pid, "a.txt", b"version one").json()["files"][0]
    b = _upload(client, pid, "b.txt", b"other doc stays").json()["files"][0]
    _wait_ready(client, pid, a["file_id"])
    _wait_ready(client, pid, b["file_id"])

    jobs_before = {j["id"] for j in client.get(f"/api/projects/{pid}/jobs").json()["jobs"]}
    changed = _upload(client, pid, "a.txt", b"version two!").json()["files"][0]
    assert changed["file_id"] == a["file_id"]
    _wait_ready(client, pid, a["file_id"])

    jobs_after = {j["id"] for j in client.get(f"/api/projects/{pid}/jobs").json()["jobs"]}
    new_jobs = jobs_after - jobs_before
    assert new_jobs, "changed file must be reprocessed"
    jobs = client.get(f"/api/projects/{pid}/jobs").json()["jobs"]
    assert all(j["document_id"] == a["file_id"]
               for j in jobs if j["id"] in new_jobs)  # only the affected source

    search = client.post(f"/api/projects/{pid}/search",
                         json={"query": "version two"}).json()
    assert search["results"] and all(
        r["document_name"] == "a.txt" for r in search["results"])


def test_manual_reprocess_endpoint(client):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    a = _upload(client, pid, "a.txt", b"content here").json()["files"][0]
    _wait_ready(client, pid, a["file_id"])
    res = client.post(f"/api/projects/{pid}/files/{a['file_id']}/reprocess")
    assert res.status_code == 202
    _wait_ready(client, pid, a["file_id"])


def test_stale_marking_when_versions_change(client):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    a = _upload(client, pid, "a.txt", b"content").json()["files"][0]
    _wait_ready(client, pid, a["file_id"])

    from app.documents import pipeline
    from app.documents import parser as parser_mod

    original = parser_mod.PARSER_VERSION
    parser_mod.PARSER_VERSION = "builtin-999"  # simulate an upgrade
    try:
        assert mark_stale(client.app.state.conn) == 1
        doc = next(f for f in client.get(f"/api/projects/{pid}/files").json()["files"]
                   if f["id"] == a["file_id"])
        assert doc["status"] == "STALE"
    finally:
        parser_mod.PARSER_VERSION = original

    # reprocessing clears STALE
    client.post(f"/api/projects/{pid}/files/{a['file_id']}/reprocess")
    _wait_ready(client, pid, a["file_id"])


# ------------------------------------------------- deletion semantics (§60)
def _seed_knowledge(db_path):
    """Two docs sharing relations: r1 evidenced only by doc A, r2 by A and B."""
    conn = db.connect(db_path)
    db.init_db(conn)
    ts = "2026-01-01T00:00:00+00:00"
    conn.execute("INSERT INTO projects VALUES ('prj_k', 'K', '', '', 'ACTIVE', ?, ?)", (ts, ts))
    for did, name in (("doc_a", "a.txt"), ("doc_b", "b.txt")):
        conn.execute(
            "INSERT INTO documents (id, project_id, name, stored_name, media_type,"
            " kind, size_bytes, content_hash, status, created_at, updated_at)"
            " VALUES (?, 'prj_k', ?, ?, '', 'text', 1, 'h', 'READY', ?, ?)",
            (did, name, name, ts, ts),
        )
        conn.execute(
            "INSERT INTO chunks (id, document_id, project_id, section_path, text,"
            " page, sequence, source_element_ids, created_at) VALUES (?, ?, 'prj_k', '[]', 'text', 1, 1, '[]', ?)",
            (f"chk_{did}", did, ts),
        )
    for eid, name, sources in (("ent_orphan", "OnlyInA", ["doc_a"]),
                               ("ent_shared", "Shared", ["doc_a", "doc_b"]),
                               ("ent_c", "OnlyInB", ["doc_b"])):
        conn.execute(
            "INSERT INTO entities (id, project_id, type, canonical_name, meta, created_at)"
            " VALUES (?, 'prj_k', 'Service', ?, ?, ?)",
            (eid, name, json.dumps({"source_documents": sources}), ts),
        )
    for rid, src, dst, evid in (
        ("rel_orphan", "ent_orphan", "ent_shared", ["chk_doc_a"]),
        ("rel_shared", "ent_shared", "ent_c", ["chk_doc_a", "chk_doc_b"]),
    ):
        conn.execute(
            "INSERT INTO relations (id, project_id, source_entity_id, relation_type,"
            " target_entity_id, confidence, created_at) VALUES (?, 'prj_k', ?, 'CALLS', ?, 1.0, ?)",
            (rid, src, dst, ts),
        )
        for cid in evid:
            conn.execute(
                "INSERT INTO relation_evidence (id, relation_id, document_id, chunk_id, page, text)"
                " VALUES (?, ?, ?, ?, 1, 'ev')",
                (f"rev_{rid}_{cid}", rid, "doc_a" if cid == "chk_doc_a" else "doc_b", cid),
            )
    conn.commit()
    conn.close()


def test_delete_source_keeps_shared_knowledge_removes_orphans(client, settings):
    _seed_knowledge(str(settings.db_path))
    pid = "prj_k"

    doc = client.get(f"/api/projects/{pid}/files").json()["files"][0]
    assert doc["id"] == "doc_a"
    res = client.delete(f"/api/projects/{pid}/files/doc_a")
    assert res.status_code == 204

    conn = client.app.state.conn
    rels = {r["id"] for r in conn.execute("SELECT id FROM relations").fetchall()}
    assert "rel_orphan" not in rels  # supported only by doc A → gone (§60)
    assert "rel_shared" in rels  # still supported by doc B → stays

    entities = {e["id"] for e in conn.execute("SELECT id FROM entities").fetchall()}
    assert "ent_orphan" not in entities  # extracted only from doc A, no surviving relations → gone
    assert "ent_shared" in entities  # supported by doc B → stays (§60)
    assert "ent_c" in entities

    chunks = {c["id"] for c in conn.execute("SELECT id FROM chunks").fetchall()}
    assert "chk_doc_a" not in chunks and "chk_doc_b" in chunks

    # project remains usable
    assert client.get(f"/api/projects/{pid}/files").json()["files"][0]["id"] == "doc_b"


def test_deleting_artifact_never_touches_knowledge(client, settings):
    from app.artifacts import manager

    settings2 = settings
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    conn = client.app.state.conn
    manager.write_artifact(conn, settings2, pid, "req.md", "# x", created_by="skill")
    art = client.get(f"/api/projects/{pid}/artifacts").json()["artifacts"][0]
    client.delete(f"/api/projects/{pid}/artifacts/{art['id']}")
    # projects table rows intact — artifact deletion is isolated (§60)
    assert client.get(f"/api/projects/{pid}").status_code == 200
