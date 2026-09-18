"""AI memory: sessions stay separate, memory is durable, evidence-referenced,
deduplicated, bounded at injection, and user-manageable (spec §26, §27)."""
from __future__ import annotations

import json

from app.agent.tools import ToolContext, execute
from app.knowledge import memory
from tests.test_harness import FakeGateway, _seed_project, resp
from app.agent.harness import HarnessRequest, NativeHarness
from app.llm.base import LLMResponse


def _pid(client):
    return client.post("/api/projects", json={"name": "M"}).json()["id"]


def test_memory_crud_and_archive(client):
    pid = _pid(client)
    created = client.post(f"/api/projects/{pid}/memories",
                          json={"content": "Deploy target is staging only.", "kind": "decision"})
    assert created.status_code == 201
    mid = created.json()["id"]

    # duplicate content is deduplicated, not stored twice
    dup = client.post(f"/api/projects/{pid}/memories",
                      json={"content": "  deploy target is STAGING only. "})
    assert dup.json()["id"] == mid

    listed = client.get(f"/api/projects/{pid}/memories").json()["memories"]
    assert len(listed) == 1 and listed[0]["kind"] == "decision"

    # archive hides it from the default list but keeps the record
    client.patch(f"/api/projects/{pid}/memories/{mid}", json={"status": "archived"})
    assert client.get(f"/api/projects/{pid}/memories").json()["memories"] == []
    archived = client.get(
        f"/api/projects/{pid}/memories?include_archived=true").json()["memories"]
    assert len(archived) == 1 and archived[0]["status"] == "archived"

    # hard delete removes it
    client.patch(f"/api/projects/{pid}/memories/{mid}", json={"status": "active"})
    assert client.delete(f"/api/projects/{pid}/memories/{mid}").status_code == 204
    assert client.get(f"/api/projects/{pid}/memories").json()["memories"] == []


def test_invalid_kind_and_empty_content_rejected(client):
    pid = _pid(client)
    assert client.post(f"/api/projects/{pid}/memories",
                       json={"content": "x", "kind": "vibes"}).status_code == 422
    assert client.post(f"/api/projects/{pid}/memories",
                       json={"content": "   "}).status_code == 422


def test_remember_tool_dedupes_and_records_source_refs(client):
    settings = client.app.state.settings
    conn = _seed_project(settings)
    ctx = ToolContext(conn, settings, "prj_1", session_id="ses_9", run_id="run_9")

    out = execute(ctx, "remember", {"content": "API gateway uses mTLS internally", "kind": "fact"})
    assert "error" not in out
    again = execute(ctx, "remember", {"content": "api gateway uses mTLS internally"})
    assert again["memory_id"] == out["memory_id"]  # dedup across agent turns

    stored = memory.get_memory(conn, "prj_1", out["memory_id"])
    assert stored["source"] == "agent"
    assert stored["source_refs"] == {"session_id": "ses_9", "run_id": "run_9"}


def test_memory_injected_into_agent_system_prompt_and_isolated(client):
    settings = client.app.state.settings
    conn = _seed_project(settings)
    memory.create_memory(conn, "prj_1", "All requirement IDs use the prefix CX-.", kind="preference")

    # a second project's memory must not leak (NFR-006)
    conn.execute(
        "INSERT INTO projects VALUES ('prj_other', 'O', '', '', 'ACTIVE', 't', 't')")
    memory.create_memory(conn, "prj_other", "Other project secret memory.")

    script = [resp(content="ok, noted the convention.")]
    req = HarnessRequest(
        conn=conn, settings=settings, client=FakeGateway(script),
        project_id="prj_1", session_id="s1", user_message="hi",
        user_message_id="m1",
    )
    events = list(NativeHarness().run(req))
    system_prompt = events[-1][1] and json.dumps([
        m.to_openai() for m in req.__dict__.get("messages", [])
    ]) if False else None

    # inspect what the harness actually sent
    sent = None
    class Capture(FakeGateway):
        def stream(self, messages, tools=None, max_output_tokens=None):
            nonlocal sent
            sent = messages
            return iter([{"response": self.script.pop(0)}])

    req2 = HarnessRequest(
        conn=conn, settings=settings, client=Capture([resp(content="fine")]),
        project_id="prj_1", session_id="s2", user_message="hi",
        user_message_id="m2",
    )
    list(NativeHarness().run(req2))
    system = sent[0].content
    assert "CX-" in system  # own memory injected
    assert "Other project secret memory." not in system  # isolation

    # archived memory is not injected
    row = conn.execute("SELECT id FROM memories WHERE content LIKE '%CX-%'").fetchone()
    memory.update_memory(conn, "prj_1", row["id"], status="archived")
    sent2 = None
    class Capture2(FakeGateway):
        def stream(self, messages, tools=None, max_output_tokens=None):
            nonlocal sent2
            sent2 = messages
            return iter([{"response": self.script.pop(0)}])
    req3 = HarnessRequest(
        conn=conn, settings=settings, client=Capture2([resp(content="fine")]),
        project_id="prj_1", session_id="s3", user_message="hi",
        user_message_id="m3",
    )
    list(NativeHarness().run(req3))
    assert "CX-" not in sent2[0].content


def test_agent_can_remember_during_a_run(client):
    settings = client.app.state.settings
    conn = _seed_project(settings)
    script = [
        LLMResponse(content=None, tool_calls=[__import__("app.llm.base", fromlist=["ToolCall"]).ToolCall(
            id="c1", name="remember",
            arguments=json.dumps({"content": "The user prefers formal Thai in outputs.", "kind": "preference"}))],
            finish_reason="tool_calls"),
        resp(content="I will remember that."),
    ]
    req = HarnessRequest(
        conn=conn, settings=settings, client=FakeGateway(script),
        project_id="prj_1", session_id="ses_mem", user_message="จำไว้ว่าชอบภาษาไทยแบบทางการ",
        user_message_id="m1",
    )
    events = list(NativeHarness().run(req))
    assert events[-1][1]["status"] == "SUCCEEDED"

    memories = memory.list_memories(conn, "prj_1")
    assert len(memories) == 1
    assert "formal Thai" in memories[0]["content"]  # agent summarized the request
    assert memories[0]["source"] == "agent"
    assert memories[0]["source_refs"]["session_id"] == "ses_mem"

    # the memory survives into a brand-new session's system prompt
    sent = None
    class Capture(FakeGateway):
        def stream(self, messages, tools=None, max_output_tokens=None):
            nonlocal sent
            sent = messages
            return iter([{"response": self.script.pop(0)}])
    req2 = HarnessRequest(
        conn=conn, settings=settings, client=Capture([resp(content="ok")]),
        project_id="prj_1", session_id="ses_new", user_message="hello",
        user_message_id="m2",
    )
    list(NativeHarness().run(req2))
    assert "formal Thai" in sent[0].content  # cross-session memory in action
