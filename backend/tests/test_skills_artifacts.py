"""Ticket #8: skill registry/runtime + artifact manager/versioning."""
from __future__ import annotations

import httpx


def _gateway(handler):
    return httpx.MockTransport(handler)


def chat(content=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message = {"role": "assistant", "content": None, "tool_calls": tool_calls}
    return httpx.Response(200, json={
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {},
    })


def tc(name, args: dict, id="call_1"):
    return {"id": id, "type": "function",
            "function": {"name": name, "arguments": __import__("json").dumps(args)}}


def _project_with_profile(client, handler):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    client.post("/api/settings/llm-profiles", json={
        "name": "gw", "base_url": "http://gw.local/v1", "model": "m", "api_key": "k",
    })
    client.app.state.llm_transport = _gateway(handler)
    return pid


def test_builtin_skill_registered_and_enabled_by_default(client):
    all_skills = client.get("/api/skills").json()["skills"]
    assert any(s["id"] == "summarizer" and s["builtin"] for s in all_skills)

    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    skills = client.get(f"/api/projects/{pid}/skills").json()["skills"]
    summarizer = next(s for s in skills if s["id"] == "summarizer")
    assert summarizer["enabled"] is True


def test_skill_toggle(client):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    res = client.put(f"/api/projects/{pid}/skills/summarizer/enabled",
                     json={"enabled": False})
    assert res.json()["enabled"] is False
    res = client.put(f"/api/projects/{pid}/skills/summarizer/enabled",
                     json={"enabled": True})
    assert res.json()["enabled"] is True


def test_explicit_skill_run_creates_versioned_artifact(client):
    """End-to-end: /skills/summarizer/run → skill writes artifact → v1 exists,
    file on disk, skill events streamed, skill_run recorded."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return chat(tool_calls=[tc("write_artifact", {
                "file_name": "summary.md",
                "content": "# Project Summary\n\nAuth flow documented [chk_0001].",
            })])
        return chat(content="Summary written to summary.md.")

    pid = _project_with_profile(client, handler)

    # seed one indexed chunk so the project has context
    from app import db as db_mod

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
        " VALUES ('chk_0001', 'doc_1', ?, '[]', 'auth flow text', 1, 1, '[]', ?)",
        (pid, ts),
    )
    conn.execute("INSERT INTO chunks_fts (chunk_id, text, section_path) VALUES ('chk_0001', 'auth flow text', '[]')")
    conn.commit()

    events = []
    res = client.post(f"/api/projects/{pid}/skills/summarizer/run",
                      json={"instruction": "Summarize the project context"})
    for block in res.text.split("\n\n"):
        if block.strip():
            lines = block.strip().split("\n")
            etype = next(l for l in lines if l.startswith("event: "))[len("event: "):]
            events.append(etype)
    assert "skill.started" in events
    assert "skill.completed" in events
    assert events[-1] == "run.completed"

    artifacts = client.get(f"/api/projects/{pid}/artifacts").json()["artifacts"]
    assert len(artifacts) == 1
    assert artifacts[0]["file_name"] == "summary.md"
    assert artifacts[0]["current_version"] == 1
    assert artifacts[0]["skill_id"] == "summarizer"

    # file written to outputs/
    from app.storage import filesystem as fs

    out = (fs.outputs_dir(client.app.state.settings.workspace_root, pid) / "summary.md")
    assert out.exists()

    # skill events persisted in messages of the skill session
    sessions = client.get(f"/api/projects/{pid}/sessions").json()["sessions"]
    assert any(s["title"].startswith("Skill:") for s in sessions)


def test_artifact_user_edit_creates_version_preserving_history(client, settings):
    from app.artifacts import manager
    from app import db as _  # noqa: F401

    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    conn = client.app.state.conn
    manager.write_artifact(conn, settings, pid, "requirement.md", "v1 content", created_by="skill")

    art = client.get(f"/api/projects/{pid}/artifacts").json()["artifacts"][0]
    res = client.put(f"/api/projects/{pid}/artifacts/{art['id']}",
                     json={"content": "v2 with user edits"})
    assert res.json()["current_version"] == 2

    detail = client.get(f"/api/projects/{pid}/artifacts/{art['id']}").json()
    assert [v["version"] for v in detail["versions"]] == [1, 2]
    assert detail["versions"][0]["created_by"] == "skill"
    assert detail["versions"][1]["created_by"] == "user"

    v1 = client.get(f"/api/projects/{pid}/artifacts/{art['id']}/content?version=1").json()
    assert v1["content"] == "v1 content"
    current = client.get(f"/api/projects/{pid}/artifacts/{art['id']}/content").json()
    assert current["content"] == "v2 with user edits"


def test_skill_run_without_profile_fails_actionably(client):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    res = client.post(f"/api/projects/{pid}/skills/summarizer/run",
                      json={"instruction": "summarize"})
    assert "run.failed" in res.text
    assert "profile" in res.text.lower()


def test_skill_tool_permissions_enforced(client):
    """A skill whose manifest doesn't declare write_artifact cannot write."""
    from app.skills import loader
    from app.skills.runtime import execute_skill_gen
    from app.agent.tools import ToolContext
    from app.agent.harness import HarnessRequest
    from tests.test_harness import FakeGateway, _seed_project
    from app.llm.base import LLMResponse, ToolCall
    import json as jsonlib

    settings = client.app.state.settings
    conn = _seed_project(settings)

    # craft a restricted skill manifest on disk (data skills root)
    skill_dir = settings.data_dir / "skills" / "readonly"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text(
        "id: readonly\nname: ReadOnly\nversion: 1.0.0\ntools: [list_project_files]\n",
        encoding="utf-8",
    )
    skill = loader.load_skill_from_dir(skill_dir, builtin=False)

    class FakeClient:
        tool_calling_mode = "native"

        def complete(self, messages, tools=None, max_output_tokens=None):
            return LLMResponse(content=None, tool_calls=[
                ToolCall(id="c1", name="write_artifact",
                         arguments=jsonlib.dumps({"file_name": "x.md", "content": "nope"}))
            ], finish_reason="tool_calls")

        def stream(self, messages, tools=None, max_output_tokens=None):
            return iter([{"response": self.complete(messages)}])

    req = HarnessRequest(
        conn=conn, settings=settings, client=FakeClient(),
        project_id="prj_1", session_id="s", user_message="go",
        user_message_id="m1",
    )
    ctx = ToolContext(conn, settings, "prj_1")
    gen = execute_skill_gen(req, ctx, skill, "write something", "run_1")
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        result = stop.value
    assert result["artifacts"] == []
    assert "not declared" in jsonlib.dumps(result) or result["summary"] != "nope"
    assert not (settings.workspace_root / "prj_1" / "outputs" / "x.md").exists()
