"""Ticket #10: evidence traceability across tools, answers, artifacts
(spec §15, §24, FR-011, Scenario F)."""
from __future__ import annotations

from app.agent.tools import ToolContext, execute
from tests.test_harness import _doc, _chunk, _seed_project


def _seed(client):
    settings = client.app.state.settings
    conn = _seed_project(settings)
    return conn, settings


def test_chunk_evidence_endpoint_resolves_to_source(client):
    _seed(client)
    pid = "prj_1"
    res = client.get(f"/api/projects/{pid}/evidence/chk_0001")
    assert res.status_code == 200
    body = res.json()
    assert body["document"] == "SRS.docx"
    assert body["page"] == 14
    assert "cxgateway" in body["text"]
    assert body["section_path"] == []


def test_chunk_evidence_404_for_unknown_or_foreign(client):
    _seed(client)
    assert client.get("/api/projects/prj_1/evidence/chk_nope").status_code == 404


def test_resolve_evidence_reports_missing(client):
    _seed(client)
    res = client.post("/api/projects/prj_1/evidence/resolve",
                      json={"chunk_ids": ["chk_0001", "chk_missing"]})
    body = res.json()
    assert len(body["evidence"]) == 1
    assert body["missing"] == ["chk_missing"]


def test_artifact_evidence_links_citations_to_sources(client, settings):
    from app.artifacts import manager

    _seed(client)
    pid = "prj_1"
    conn = client.app.state.conn
    manager.write_artifact(
        conn, settings, pid, "requirement.md",
        "The gateway shall forward auth requests [chk_0001].",
        source_context=[{"document_id": "doc_1", "chunk_id": "chk_0001", "page": 14}],
        created_by="skill",
    )
    art = client.get(f"/api/projects/{pid}/artifacts").json()["artifacts"][0]
    res = client.get(f"/api/projects/{pid}/artifacts/{art['id']}/evidence")
    body = res.json()
    assert body["citations"][0]["document"] == "SRS.docx"
    assert body["citations"][0]["page"] == 14
    assert body["unresolved"] == []

    # citations that no longer resolve are surfaced, not hidden
    manager.write_artifact(
        conn, settings, pid, "broken.md", "claims [chk_gone] exist",
        created_by="skill",
    )
    art2 = client.get(f"/api/projects/{pid}/artifacts").json()["artifacts"]
    broken = next(a for a in art2 if a["file_name"] == "broken.md")
    body2 = client.get(f"/api/projects/{pid}/artifacts/{broken['id']}/evidence").json()
    assert body2["unresolved"] == [{"chunk_id": "chk_gone"}]


def test_get_evidence_tool_is_project_scoped(client):
    settings = client.app.state.settings
    conn = _seed_project(settings)
    ctx = ToolContext(conn, settings, "prj_1")
    out = execute(ctx, "get_evidence", {"chunk_id": "chk_0001"})
    assert out["document"] == "SRS.docx"
    assert "error" in execute(ctx, "get_evidence", {"chunk_id": "chk_other_project"})
