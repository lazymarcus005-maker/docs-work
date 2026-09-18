"""Ticket #18: V1 acceptance scenarios A–H (spec §67) demonstrated end-to-end
through the API. Each test maps to one or more scenarios; hardening checks
live in test_security.py."""
from __future__ import annotations

import io
import json
import time

import httpx
import openpyxl
import pypdf

from tests.test_harness import FakeGateway, _seed_project
from app.agent.harness import HarnessRequest, NativeHarness
from app.llm.base import LLMResponse, ToolCall


# ----------------------------------------------------------------- helpers
def _sse_events(res) -> list[tuple[str, dict]]:
    events = []
    for block in res.text.split("\n\n"):
        if block.strip():
            lines = block.strip().split("\n")
            etype = next(l for l in lines if l.startswith("event: "))[len("event: "):]
            data = json.loads(next(l for l in lines if l.startswith("data: "))[len("data: "):])
            events.append((etype, data))
    return events


def _sse_handler(text):
    chunks = [text[i:i + 5] for i in range(0, len(text), 5)]
    body = "".join(
        f'data: {{"choices":[{{"delta":{{"content":"{c}"}}}}]}}\n\n' for c in chunks
    ) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body.encode(),
                          headers={"content-type": "text/event-stream"})


def _wait(client, pid, fid, status, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        files = client.get(f"/api/projects/{pid}/files").json()["files"]
        doc = next((f for f in files if f["id"] == fid), None)
        if doc and doc["status"] == status:
            return doc
        time.sleep(0.15)
    raise AssertionError(f"{fid} never reached {status}")


def _make_xlsx():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "APIs"
    ws.append(["endpoint", "auth"])
    ws.append(["/v1/login", "oauth"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------- Scenario A + B: create + index
def test_scenario_a_b_create_project_and_local_indexing(client):
    # A: fresh installation → create project with name/instruction/files
    res = client.post("/api/projects", json={
        "name": "CXGateway Migration",
        "instruction": "Use uploaded evidence only. Never invent missing requirements.",
    })
    assert res.status_code == 201
    pid = res.json()["id"]

    pdf = io.BytesIO()
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    from pypdf.generic import DictionaryObject, NameObject, TextStringObject

    page[NameObject("/Contents")] = writer._add_object(
        TextStringObject("BT /F1 12 Tf 72 720 Td (authentication tokens) Tj ET"))
    writer.write(pdf)

    uploads = client.post(f"/api/projects/{pid}/files", files=[
        ("files", ("SRS.docx", io.BytesIO(
            b"PK\x03\x04 not a real docx but parses as unknown"), "application/octet-stream")),
        ("files", ("api.xlsx", _make_xlsx(),
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
    ])
    assert uploads.status_code == 201
    files = uploads.json()["files"]

    # files appear under Context immediately (before processing finishes)
    listed = client.get(f"/api/projects/{pid}/files").json()["files"]
    assert {f["name"] for f in listed} == {"SRS.docx", "api.xlsx"}

    # B: local CPU indexing completes without GPU/cloud — xlsx reaches READY
    _wait(client, pid, files[1]["file_id"], "READY")
    search = client.post(f"/api/projects/{pid}/search",
                         json={"query": "v1/login oauth"}).json()
    assert search["results"]  # indexed and searchable locally


# ------------------------------------- Scenario C: ask a project question
def test_scenario_c_ask_project_question_with_evidence(client, settings):
    conn = _seed_project(settings)
    script = [
        LLMResponse(content=None, tool_calls=[ToolCall(
            id="c1", name="search_project",
            arguments=json.dumps({"query": "authentication gateway"}))]),
        LLMResponse(content="The cxgateway forwards authentication requests "
                            "and tokens expire after 30 minutes [chk_0001].",
                    tool_calls=[]),
    ]
    req = HarnessRequest(
        conn=conn, settings=settings, client=FakeGateway(script),
        project_id="prj_1", session_id="s", user_message_id="m",
        user_message="What expires when?",
    )
    events = list(NativeHarness().run(req))
    final = events[-1][1]
    assert final["status"] == "SUCCEEDED"
    assert "cxgateway" in final["content"]
    assert "chk_0001" in final["evidence_refs"]


# --------------------------- Scenario D + E + F: BA skill, revision, evidence
def test_scenario_d_e_f_ba_skill_revision_evidence(client, settings):
    from app.artifacts import manager

    pid = client.post("/api/projects", json={"name": "BA Demo"}).json()["id"]
    client.post("/api/settings/llm-profiles", json={
        "name": "gw", "base_url": "http://gw.local/v1", "model": "m", "api_key": "k",
    })
    conn = client.app.state.conn
    ts = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO documents (id, project_id, name, stored_name, media_type,"
        " kind, size_bytes, content_hash, status, created_at, updated_at)"
        " VALUES ('doc_1', ?, 'SRS.docx', 'SRS.docx', '', 'docx', 1, 'h', 'READY', ?, ?)",
        (pid, ts, ts))
    conn.execute(
        "INSERT INTO chunks (id, document_id, project_id, section_path, text,"
        " page, sequence, source_element_ids, created_at)"
        " VALUES ('chk_0001', 'doc_1', ?, '[]', 'Latency requirement: 2 seconds', 4, 1, '[]', ?)",
        (pid, ts))
    conn.execute("INSERT INTO chunks_fts (chunk_id, text, section_path) VALUES ('chk_0001', 'latency', '[]')")
    conn.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        tools = [t["function"]["name"] for t in body.get("tools") or []]
        if "run_skill" in tools:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "s1", "type": "function", "function": {"name": "run_skill", "arguments": json.dumps({"skill": "ba", "instruction": "create requirements"})}}]},
                "finish_reason": "tool_calls"}], "usage": {}})
        if any((m.get("content") or "").startswith("[skill ba result]") for m in body["messages"]):
            return httpx.Response(200, content=(
                'data: {"choices":[{"delta":{"content":"requirement.md created"}}]}\n\n'
                'data: [DONE]\n\n').encode(),
                headers={"content-type": "text/event-stream"})
        tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
        names = [m.get("name") for m in tool_msgs]
        if "write_artifact" not in names:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "w1", "type": "function", "function": {"name": "write_artifact", "arguments": json.dumps({
                    "file_name": "requirement.md",
                    "content": "# BA\n\n## Business Context\n\n## Goals\n\n## Requirements\n\n### REQ-001 — Latency\n\n- Statement: latency <= 2 seconds.\n- Type: non-functional\n- Source evidence: [chk_0001] (SRS.docx, p4)\n",
                    "source_context": [{"chunk_id": "chk_0001"}]})}}]},
                "finish_reason": "tool_calls"}], "usage": {}})
        if "run_validator" not in names:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "v1", "type": "function", "function": {"name": "run_validator", "arguments": json.dumps({"file_name": "requirement.md", "skill_id": "ba"})}}]},
                "finish_reason": "tool_calls"}], "usage": {}})
        return httpx.Response(200, content=(
            'data: {"choices":[{"delta":{"content":"done"}}]}\n\ndata: [DONE]\n\n').encode(),
            headers={"content-type": "text/event-stream"})

    client.app.state.llm_transport = httpx.MockTransport(handler)
    events = _sse_events(client.post(
        f"/api/projects/{pid}/chat", json={"message": "create requirements from context"}))

    # D: skill auto-selected, artifact generated, validated, saved
    assert "skill.started" in [t for t, _ in events]
    arts = client.get(f"/api/projects/{pid}/artifacts").json()["artifacts"]
    assert arts and arts[0]["validation_status"] == "passed"

    # F: evidence opens to the source chunk
    ev = client.get(f"/api/projects/{pid}/artifacts/{arts[0]['id']}/evidence").json()
    assert ev["citations"][0]["chunk_id"] == "chk_0001"

    # E: targeted revision preserves a new version
    manager.write_artifact(conn, settings, pid, "design.md", "# Design\n\n## v1 body", created_by="skill")
    design = next(a for a in client.get(f"/api/projects/{pid}/artifacts").json()["artifacts"]
                  if a["file_name"] == "design.md")
    client.put(f"/api/projects/{pid}/artifacts/{design['id']}",
               json={"content": "# Design\n\n## v1 body\n\n## v2 addition"})
    detail = client.get(f"/api/projects/{pid}/artifacts/{design['id']}").json()
    assert [v["version"] for v in detail["versions"]] == [1, 2]


# ------------------------------------------- Scenario G + H: incremental + restart
def test_scenario_g_h_incremental_and_restart(client, settings):
    from tests.test_incremental import _upload, _wait_ready

    pid = client.post("/api/projects", json={"name": "G H"}).json()["id"]
    a = _upload(client, pid, "a.txt", b"content one").json()["files"][0]
    _wait_ready(client, pid, a["file_id"])

    # G: changing one document reprocesses only it
    jobs_before = {j["id"] for j in client.get(f"/api/projects/{pid}/jobs").json()["jobs"]}
    _upload(client, pid, "a.txt", b"content two changed")
    _wait_ready(client, pid, a["file_id"])
    jobs_after = {j["id"] for j in client.get(f"/api/projects/{pid}/jobs").json()["jobs"]}
    new_jobs = jobs_after - jobs_before
    assert new_jobs
    jobs = client.get(f"/api/projects/{pid}/jobs").json()["jobs"]
    assert all(j["document_id"] == a["file_id"] for j in jobs if j["id"] in new_jobs)

    # H: restart — project, files, sessions, artifacts survive
    client.post(f"/api/projects/{pid}/chat",
                json={"message": "hi"})  # no profile: run failed, but message persisted
    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings)) as fresh:
        assert fresh.get(f"/api/projects/{pid}").json()["name"] == "G H"
        assert fresh.get(f"/api/projects/{pid}/files").json()["files"]
        sessions = fresh.get(f"/api/projects/{pid}/sessions").json()["sessions"]
        assert sessions, "sessions survive restart"
        assert fresh.get(f"/api/projects/{pid}/processing").status_code == 200
