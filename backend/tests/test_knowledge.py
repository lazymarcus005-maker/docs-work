"""Ticket #12: knowledge extraction, entity resolution, review queue."""
from __future__ import annotations

import io
import time

from app.knowledge.extraction import extract_from_text
from app.knowledge.resolver import find_or_create_entity, merge_entities
from app.storage import filesystem as fs


def test_extraction_finds_typed_entities_and_relations():
    text = ("The cxgateway forwards authentication requests to cxntlappux. "
            "The cxntlappux stores session state in Redis. See REQ-001.")
    out = extract_from_text(text)
    by_type = {}
    for m in out.mentions:
        by_type.setdefault(m.type, set()).add(m.name.lower())
    assert "redis" in by_type.get("Technology", set())
    assert any("gateway" in n for n in by_type.get("Service", set()))
    assert any("REQ-001" == n.upper() for n in by_type.get("Requirement", set()))
    rels = {(r.source.lower(), r.relation_type, r.target.lower()) for r in out.relations}
    assert any(rt == "CALLS" for (_s, rt, _t) in rels)
    assert any(rt == "CONSUMES" for (_s, rt, _t) in rels)


def test_extraction_conflict_value_detected():
    out = extract_from_text("Token expires after 30 minutes.")
    assert out.conflicts == [{"subject": "token_lifetime_minutes", "value": 30}]


def test_resolution_normalizes_aliases_to_one_entity():
    from app import db
    from app.config import Settings
    import tempfile

    settings = Settings(env={"COWORK_DATA_DIR": tempfile.mkdtemp()})
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO projects (id, name, description, instruction, status, created_at, updated_at, autonomy_level) VALUES ('p', 'P', '', '', 'ACTIVE', 't', 't', 2)")

    ids = set()
    for alias in ("cxgateway", "CX Gateway", "CXGateway", "cx-gateway"):
        eid, _ = find_or_create_entity(conn, "p", alias, "Service", "doc_1")
        ids.add(eid)
    assert len(ids) == 1  # all alias spellings resolve together (§14)

    aliases = {a["alias"] for a in conn.execute(
        "SELECT alias FROM entity_aliases WHERE entity_id = ?", (ids.pop(),)).fetchall()}
    assert aliases == {"cxgateway", "CX Gateway", "CXGateway", "cx-gateway"}


def test_weak_similarity_becomes_review_item_not_merge():
    from app import db
    from app.config import Settings
    import tempfile

    settings = Settings(env={"COWORK_DATA_DIR": tempfile.mkdtemp()})
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO projects (id, name, description, instruction, status, created_at, updated_at, autonomy_level) VALUES ('p', 'P', '', '', 'ACTIVE', 't', 't', 2)")

    a, _ = find_or_create_entity(conn, "p", "ReportingService", "Service")
    b, _ = find_or_create_entity(conn, "p", "ReportingServcies", "Service")  # weak match
    assert a != b  # not silently merged (§14)
    items = conn.execute("SELECT kind FROM review_items WHERE project_id='p'").fetchall()
    assert any(i["kind"] == "duplicate_entity" for i in items)


def test_merge_entities_folds_aliases_and_relations():
    from app import db
    from app.config import Settings
    import tempfile

    settings = Settings(env={"COWORK_DATA_DIR": tempfile.mkdtemp()})
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO projects (id, name, description, instruction, status, created_at, updated_at, autonomy_level) VALUES ('p', 'P', '', '', 'ACTIVE', 't', 't', 2)")
    a, _ = find_or_create_entity(conn, "p", "PaymentService", "Service")
    c, _ = find_or_create_entity(conn, "p", "Ledger", "Service")
    # a separate duplicate entity (as a review-item merge would have)
    conn.execute(
        "INSERT INTO entities (id, project_id, type, canonical_name, meta, created_at)"
        " VALUES ('ent_dup', 'p', 'Service', 'PaymentServise', '{}', 't')")
    conn.execute(
        "INSERT INTO entity_aliases (entity_id, project_id, alias, norm_alias, created_at)"
        " VALUES ('ent_dup', 'p', 'PaymentServise', 'paymentservise', 't')")
    conn.execute(
        "INSERT INTO relations (id, project_id, source_entity_id, relation_type,"
        " target_entity_id, confidence, created_at) VALUES ('r1', 'p', 'ent_dup', 'CALLS', ?, 1, 't')",
        (c,))

    merge_entities(conn, "p", a, "ent_dup")
    names = {r["canonical_name"] for r in conn.execute(
        "SELECT canonical_name FROM entities WHERE project_id='p'").fetchall()}
    assert names == {"PaymentService", "Ledger"}
    src = conn.execute("SELECT source_entity_id FROM relations WHERE id='r1'").fetchone()
    assert src["source_entity_id"] == a


# ------------------------------------------------- end-to-end via upload
def _upload(client, pid, name, content):
    return client.post(
        f"/api/projects/{pid}/files",
        files={"files": (name, io.BytesIO(content), "text/plain")},
    )


def _wait(client, pid, predicate, timeout=12.0, what="condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.15)
    raise AssertionError(f"timeout waiting for {what}: {last}")


def test_upload_pipeline_extracts_entities_relations_with_evidence(client):
    pid = client.post("/api/projects", json={"name": "K"}).json()["id"]
    _upload(client, pid, "arch.txt",
            b"The cxgateway forwards authentication requests to cxntlappux. "
            b"Session state is cached in Redis. Token expires after 30 minutes.")
    _upload(client, pid, "api.txt",
            b"The cxntlappux calls Redis for sessions. Token expires after 60 minutes.")

    def knowledge_ready():
        ents = client.get(f"/api/projects/{pid}/entities").json()["entities"]
        rels = client.get(f"/api/projects/{pid}/relations").json()["relations"]
        if len(ents) >= 2 and rels:
            return ents, rels
        return None

    ents, rels = _wait(client, pid, knowledge_ready, what="knowledge")
    names = {e["canonical_name"].lower() for e in ents}
    assert any("cxgateway" in n for n in names)
    assert any("cxntlappux" in n for n in names)
    assert "redis" in names

    # every relation carries evidence — no evidence-free edges (§15)
    for rel in rels:
        assert rel["evidence"], f"relation {rel['id']} has no evidence"
        assert rel["evidence"][0]["chunk_id"].startswith("chk_")

    # conflicting lifetimes recorded, never silently merged (§47)
    conflicts = client.get(f"/api/projects/{pid}/conflicts").json()["conflicts"]
    assert conflicts and conflicts[0]["subject"] == "token_lifetime_minutes"
    values = {d["value"] for d in conflicts[0]["details"]}
    assert values == {30, 60}

    # conflict surfaces in the review queue
    review = client.get(f"/api/projects/{pid}/review").json()["items"]
    assert any(i["kind"] == "conflict" for i in review)


def test_review_queue_merge_action(client):
    from app import db

    pid = client.post("/api/projects", json={"name": "R"}).json()["id"]
    conn = client.app.state.conn
    ts = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO entities (id, project_id, type, canonical_name, meta, created_at)"
        " VALUES ('ent_a', ?, 'Service', 'PaymentService', '{}', ?)", (pid, ts))
    conn.execute(
        "INSERT INTO entities (id, project_id, type, canonical_name, meta, created_at)"
        " VALUES ('ent_b', ?, 'Service', 'PaymentServise', '{}', ?)", (pid, ts))
    conn.execute(
        "INSERT INTO review_items (id, project_id, kind, payload, suggestion, status, created_at)"
        " VALUES ('rev_1', ?, 'duplicate_entity', ?, ?, 'open', ?)",
        (pid, '{"entity_ids": ["ent_a", "ent_b"], "names": ["PaymentService", "PaymentServise"]}',
         '{"action": "merge", "into": "ent_a"}', ts))
    conn.commit()

    res = client.post(f"/api/projects/{pid}/review/rev_1/resolve", json={"action": "merge"})
    assert res.json()["status"] == "resolved"
    names = {e["canonical_name"] for e in
             client.get(f"/api/projects/{pid}/entities").json()["entities"]}
    assert names == {"PaymentService"}
    detail = client.get(f"/api/projects/{pid}/entities/ent_a").json()
    assert "PaymentServise" in detail["aliases"]
    assert client.get(f"/api/projects/{pid}/review").json()["items"] == []
