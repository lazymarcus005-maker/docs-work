"""Ticket #13: graph-aware agent tools + Knowledge Explorer backend
(spec §20, §30, §56, §72)."""
from __future__ import annotations

import json
import threading

from app.agent import tools as tools_mod
from app.agent.harness import HarnessRequest, NativeHarness
from app.agent.tools import ToolContext, execute
from app.llm.base import LLMResponse, ToolCall
from tests.test_harness import FakeGateway, _doc, _chunk, _seed_project


def _seed_graph(settings):
    conn = _seed_project(settings, with_content=True)
    ts = "2026-01-01T00:00:00+00:00"
    for eid, name, aliases in (("ent_gw", "cxgateway", ("cxgateway", "CX Gateway", "CXGateway")),
                               ("ent_ux", "cxntlappux", ("cxntlappux",)),
                               ("ent_redis", "redis", ("redis",))):
        conn.execute(
            "INSERT INTO entities (id, project_id, type, canonical_name, meta, created_at)"
            " VALUES (?, 'prj_1', 'Service', ?, '{}', ?)", (eid, name, ts))
        for alias in aliases:
            conn.execute(
                "INSERT INTO entity_aliases (entity_id, project_id, alias, norm_alias, created_at)"
                " VALUES (?, 'prj_1', ?, ?, ?)", (eid, alias, alias.lower(), ts))
    for rid, src, rel, dst, ev in (
        ("rel_1", "ent_gw", "CALLS", "ent_ux", "chk_0001"),
        ("rel_2", "ent_ux", "USES", "ent_redis", "chk_0001"),
    ):
        conn.execute(
            "INSERT INTO relations (id, project_id, source_entity_id, relation_type,"
            " target_entity_id, confidence, created_at) VALUES (?, 'prj_1', ?, ?, ?, 0.95, ?)",
            (rid, src, rel, dst, ts))
        conn.execute(
            "INSERT INTO relation_evidence (id, relation_id, document_id, chunk_id, page, text)"
            " VALUES (?, ?, 'doc_1', ?, 14, 'the evidence text')", (f"rev_{rid}", rid, ev))
    conn.commit()
    return conn


def test_find_entity_and_query_graph_tools(settings):
    conn = _seed_graph(settings)
    ctx = ToolContext(conn, settings, "prj_1")

    found = execute(ctx, "find_entity", {"query": "gateway"})
    assert any(e["canonical_name"] == "cxgateway" for e in found["entities"])

    # alias lookup works too
    by_alias = execute(ctx, "find_entity", {"query": "CX Gateway"})
    assert any(e["id"] == "ent_gw" for e in by_alias["entities"])

    graph = execute(ctx, "query_graph", {"entity": "cxgateway", "depth": 2})
    edge_types = {(e["source"], e["type"], e["target"]) for e in graph["edges"]}
    assert ("cxgateway", "CALLS", "cxntlappux") in edge_types
    assert ("cxntlappux", "USES", "redis") in edge_types
    # edges carry evidence chunk ids (no evidence-free traversal)
    assert all(e["evidence_chunks"] for e in graph["edges"])

    assert "error" in execute(ctx, "query_graph", {"entity": "unknown-thing"})


def test_query_graph_is_project_scoped(settings):
    conn = _seed_graph(settings)
    other = ToolContext(conn, settings, "prj_other")
    assert "error" in execute(other, "query_graph", {"entity": "cxgateway"})
    assert execute(other, "find_entity", {"query": "redis"})["entities"] == []


def test_agent_answers_dependency_question_via_graph(settings):
    """§72 in action: the agent uses the graph, citing evidence."""
    conn = _seed_graph(settings)
    script = [
        LLMResponse(content=None, tool_calls=[ToolCall(
            id="c1", name="query_graph",
            arguments=json.dumps({"entity": "cxgateway", "depth": 2}))],
            finish_reason="tool_calls"),
        LLMResponse(content="cxgateway CALLS cxntlappux, which USES redis "
                            "(evidence chk_0001).", tool_calls=[], finish_reason="stop"),
    ]
    fake = FakeGateway(script)
    req = HarnessRequest(
        conn=conn, settings=settings, client=fake, project_id="prj_1",
        session_id="s1", user_message="what does cxgateway call?",
        user_message_id="m1",
    )
    events = list(NativeHarness().run(req))
    final = events[-1][1]
    assert final["status"] == "SUCCEEDED"
    assert "chk_0001" in final["evidence_refs"]

    observation = fake.requests[1][-1].content
    assert "cxntlappux" in observation and "USES" in observation

    run = conn.execute("SELECT tool_call_count FROM agent_runs").fetchone()
    assert run["tool_call_count"] == 1


def test_knowledge_health_indicators(client):
    settings = client.app.state.settings
    conn = _seed_graph(settings)
    ts = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO review_items (id, project_id, kind, payload, suggestion, status, created_at)"
        " VALUES ('rev_x', 'prj_1', 'duplicate_entity', '{}', '{}', 'open', ?)", (ts,))
    conn.execute(
        "INSERT INTO conflicts (id, project_id, subject, details, status, created_at)"
        " VALUES ('cfl_x', 'prj_1', 'token_lifetime_minutes', '[]', 'open', ?)", (ts,))
    conn.commit()

    health = client.get("/api/projects/prj_1/health").json()
    assert health["documents_total"] == 1
    assert health["documents_ready"] == 1
    assert health["open_review_items"] == 1
    assert health["conflicts"] == 1
    assert health["entities"] == 3 and health["relations"] == 2
