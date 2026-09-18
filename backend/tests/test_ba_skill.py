"""Ticket #9: built-in BA skill — generation, revision, validation,
/ba + natural-language invocation (FR-008, FR-018, Scenarios D/E)."""
from __future__ import annotations

import json

import httpx

from tests.test_skills_artifacts import chat, tc, _project_with_profile


def _seed_chunk(client, pid, text="Token expires after 30 minutes. The gateway forwards authentication requests to cxntlappux."):
    conn = client.app.state.conn
    ts = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO documents (id, project_id, name, stored_name, media_type,"
        " kind, size_bytes, content_hash, status, created_at, updated_at)"
        " VALUES ('doc_1', ?, 'SRS.docx', 'SRS.docx', '', 'docx', 1, 'h', 'READY', ?, ?)",
        (pid, ts, ts),
    )
    conn.execute(
        "INSERT INTO chunks (id, document_id, project_id, section_path, text,"
        " page, sequence, source_element_ids, created_at)"
        " VALUES ('chk_0001', 'doc_1', ?, '[\"Authentication\"]', ?, 4, 1, '[]', ?)",
        (pid, text, ts),
    )
    conn.execute(
        "INSERT INTO chunks_fts (chunk_id, text, section_path) VALUES ('chk_0001', ?, '[]')",
        (text,),
    )
    conn.commit()


def _outer_or_inner(body):
    """Classify a scripted gateway request: 'outer' (parent harness loop)
    vs 'inner' (BA skill loop) vs outer-with-skill-result."""
    tools = [t["function"]["name"] for t in body.get("tools") or []]
    if "run_skill" in tools:
        if any((m.get("content") or "").startswith("[skill ba result]")
               for m in body.get("messages", [])):
            return "outer_final"
        return "outer"
    return "inner"


BA_REQUIREMENT = """# CX Migration — Requirements

## Business Context
The gateway migration project covers authentication.

## Goals
Migrate the authentication gateway safely.

## Requirements

### REQ-001 — Authentication forwarding

- Statement: The system shall forward authentication requests to cxntlappux.
- Type: functional
- Source evidence: [chk_0001] (SRS.docx, p4)

### NFR-001 — Token lifetime

- Statement: Tokens shall expire after 30 minutes.
- Type: non-functional
- Source evidence: [chk_0001] (SRS.docx, p4)

## Clarification Questions
None.

## Conflicts Detected
None.
"""


def test_ba_manifest_is_complete_and_default_enabled(client):
    skills = {s["id"]: s for s in client.get("/api/skills").json()["skills"]}
    assert "ba" in skills
    ba = skills["ba"]
    assert ba["builtin"] and ba["enabled_by_default"]

    from app.skills.loader import load_skill_from_dir
    from pathlib import Path

    skill = load_skill_from_dir(
        Path(client.app.state.settings.builtin_skills_dir) / "ba", True
    )
    for rule in ("never_invent_missing_information", "prefer_project_terminology",
                 "cite_evidence_for_critical_claims", "identify_conflicts",
                 "preserve_user_edits"):
        assert rule in skill.rules
    assert "write_artifact" in skill.tools and "run_validator" in skill.tools


def test_scenario_d_generate_requirements_from_context(client):
    """Scenario D: natural-language request → agent auto-selects ba →
    requirement.md generated, validated, saved, result shown in chat."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        kind = _outer_or_inner(body)
        if kind == "outer":
            return chat(tool_calls=[tc("run_skill", {
                "skill": "ba", "instruction": "Create requirements from all context",
            }, id="s1")])
        if kind == "outer_final":
            return chat(content="BA task completed.\n\nSources used:\n- SRS.docx\n\nOutput:\nrequirement.md")
        tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
        if not tool_msgs:
            return chat(tool_calls=[tc("write_artifact", {
                "file_name": "requirement.md", "content": BA_REQUIREMENT,
                "source_context": [{"document_id": "doc_1", "chunk_id": "chk_0001", "page": 4}],
            }, id="c1")])
        if "run_validator" not in [m.get("name") for m in tool_msgs]:
            return chat(tool_calls=[tc("run_validator", {
                "file_name": "requirement.md", "skill_id": "ba"}, id="c2")])
        return chat(content="Requirements written and validated.")

    pid = _project_with_profile(client, handler)
    _seed_chunk(client, pid)

    events = []
    res = client.post(f"/api/projects/{pid}/chat",
                      json={"message": "create requirements from all context"})
    for block in res.text.split("\n\n"):
        if block.strip():
            lines = block.strip().split("\n")
            events.append(next(l for l in lines if l.startswith("event: "))[len("event: "):])

    assert "skill.started" in events and "skill.completed" in events

    artifacts = client.get(f"/api/projects/{pid}/artifacts").json()["artifacts"]
    assert artifacts[0]["file_name"] == "requirement.md"
    assert artifacts[0]["current_version"] == 1
    assert artifacts[0]["validation_status"] == "passed"
    assert artifacts[0]["skill_id"] == "ba"

    detail = client.get(f"/api/projects/{pid}/artifacts/{artifacts[0]['id']}").json()
    assert detail["versions"][0]["validation"]["validation_status"] == "passed"

    content = client.get(
        f"/api/projects/{pid}/artifacts/{artifacts[0]['id']}/content").json()["content"]
    assert "chk_0001" in content
    assert "REQ-001" in content and "NFR-001" in content

    from app.storage import filesystem as fs

    assert (fs.outputs_dir(client.app.state.settings.workspace_root, pid) / "requirement.md").exists()


def test_explicit_ba_prefix_invocation(client):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        kind = _outer_or_inner(body)
        if kind in ("outer", "outer_final"):
            return chat(content="User stories created.")  # forced skill: no outer loop work
        tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
        if not tool_msgs:
            return chat(tool_calls=[tc("write_artifact", {
                "file_name": "user-stories.md",
                "content": "# Business Context\n\n## Goals\n\n## Requirements\n\n### US-001 — Login\nAs a user, I want to log in.",
            }, id="c1")])
        if "run_validator" not in [m.get("name") for m in tool_msgs]:
            return chat(tool_calls=[tc("run_validator", {
                "file_name": "user-stories.md", "skill_id": "ba"}, id="c2")])
        return chat(content="done")

    pid = _project_with_profile(client, handler)
    _seed_chunk(client, pid)
    res = client.post(f"/api/projects/{pid}/chat",
                      json={"message": "/ba create user stories from requirement.md"})
    assert "skill.started" in res.text
    assert any(a["file_name"] == "user-stories.md"
               for a in client.get(f"/api/projects/{pid}/artifacts").json()["artifacts"])


def test_scenario_e_targeted_revision_preserves_versions_and_edits(client, settings):
    from app.artifacts import manager

    pid = _project_with_profile(client, lambda req: chat(content="unused"))
    _seed_chunk(client, pid)
    conn = client.app.state.conn

    # v1: skill-generated; v2: user edits a section manually
    manager.write_artifact(conn, settings, pid, "requirement.md", BA_REQUIREMENT,
                           skill_id="ba", created_by="skill")
    art = client.get(f"/api/projects/{pid}/artifacts").json()["artifacts"][0]
    revised = BA_REQUIREMENT.replace("Migrate the authentication gateway safely.",
                                     "Migrate the authentication gateway safely. USER NOTE: keep latency low.")
    client.put(f"/api/projects/{pid}/artifacts/{art['id']}", json={"content": revised})
    assert client.get(f"/api/projects/{pid}/artifacts/{art['id']}").json()["current_version"] == 2

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        kind = _outer_or_inner(body)
        if kind in ("outer", "outer_final"):
            return chat(content="Added NFR-002 to requirement.md.")
        tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
        names = [m.get("name") for m in tool_msgs]
        if "read_artifact" not in names:
            return chat(tool_calls=[tc("read_artifact", {"file_name": "requirement.md"}, id="r1")])
        if "update_artifact" not in names:
            updated = revised.replace(
                "## Conflicts Detected",
                "### NFR-002 — Latency\n\n- Statement: Latency shall be <= 2 seconds.\n"
                "- Type: non-functional\n- Source evidence: [chk_0001] (SRS.docx, p4)\n\n"
                "## Conflicts Detected")
            return chat(tool_calls=[tc("update_artifact", {
                "file_name": "requirement.md", "content": updated}, id="u1")])
        if "run_validator" not in names:
            return chat(tool_calls=[tc("run_validator", {
                "file_name": "requirement.md", "skill_id": "ba"}, id="v1")])
        return chat(content="Revision complete.")

    client.app.state.llm_transport = httpx.MockTransport(handler)
    client.post(f"/api/projects/{pid}/chat",
                json={"message": "/ba add NFR: latency <= 2 seconds", "skill_id": "ba"})
    detail = client.get(f"/api/projects/{pid}/artifacts/{art['id']}").json()
    assert [v["version"] for v in detail["versions"]] == [1, 2, 3]

    current = client.get(f"/api/projects/{pid}/artifacts/{art['id']}/content").json()["content"]
    assert "NFR-002" in current
    assert "USER NOTE: keep latency low." in current  # user edits preserved

    v1 = client.get(f"/api/projects/{pid}/artifacts/{art['id']}/content?version=1").json()
    assert "NFR-002" not in v1["content"]  # history intact


def test_ba_validation_flags_missing_evidence(client, settings):
    from app.artifacts import manager
    from app.skills.validator import validate_artifact

    pid = _project_with_profile(client, lambda req: chat(content="unused"))
    _seed_chunk(client, pid)
    conn = client.app.state.conn
    bad = BA_REQUIREMENT.replace("[chk_0001] (SRS.docx, p4)", "[chk_9999] (SRS.docx, p4)")
    manager.write_artifact(conn, settings, pid, "requirement.md", bad, skill_id="ba")

    result = validate_artifact(conn, settings, pid, "requirement.md", "ba")
    check = next(c for c in result["checks"] if c["check"] == "cited evidence exists")
    assert check["passed"] is False
    assert "chk_9999" in check["detail"]
