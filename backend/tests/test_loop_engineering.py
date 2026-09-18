"""Loop-engineering safeguards: independent verifier, autonomy levels L1–L3,
token budget with kill switch, and the validation fix-attempt cap."""
from __future__ import annotations

import json

import httpx

from app.agent import spend
from app.agent.harness import HarnessRequest, NativeHarness
from app.llm.base import LLMResponse, ToolCall
from tests.test_harness import FakeGateway, _seed_project, resp, tool_call


VERIFIER_JSON = json.dumps({
    "verdict": "fail",
    "issues": ["Claim '30 minutes' is not supported by cited evidence."],
    "notes": "weak citations",
})


# ------------------------------------------------------- verifier split
def test_verifier_runs_and_can_fail_artifact(client, settings):
    """Maker/checker: the verifier is a separate call and its REJECT verdict
    overrides a passing deterministic validation."""
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    client.post("/api/settings/llm-profiles", json={
        "name": "gw", "base_url": "http://gw/v1", "model": "m", "api_key": "k"})

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content)
        text = body["messages"][-1]["content"]
        if "strict artifact verifier" in (body["messages"][0]["content"] or ""):
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": VERIFIER_JSON}, "finish_reason": "stop"}],
                "usage": {}})
        if calls["n"] == 1:  # skill writes the artifact
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "write_artifact",
                "arguments": json.dumps({"file_name": "req.md",
                "content": "# Business Context\n\n## Goals\n\n## Requirements\n\n### REQ-001 — t\n\n- Statement: tokens expire in 30 minutes [chk_0001].\n"})}}]},
                "finish_reason": "tool_calls"}], "usage": {}})
        return httpx.Response(200, content=(
            'data: {"choices":[{"delta":{"content":"done"}}]}\n\ndata: [DONE]\n\n').encode(),
            headers={"content-type": "text/event-stream"})

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
        " VALUES ('chk_0001', 'doc_1', ?, '[]', 'unrelated evidence text', 1, 1, '[]', ?)",
        (pid, ts))
    conn.commit()

    client.app.state.llm_transport = httpx.MockTransport(handler)
    events = []
    res = client.post(f"/api/projects/{pid}/skills/summarizer/run",
                      json={"instruction": "write req.md"})
    for block in res.text.split("\n\n"):
        if block.strip():
            lines = block.strip().split("\n")
            events.append(next(l for l in lines if l.startswith("event: "))[len("event: "):])

    art = client.get(f"/api/projects/{pid}/artifacts").json()["artifacts"][0]
    assert art["validation_status"] == "failed"  # verifier REJECT wins
    detail = client.get(f"/api/projects/{pid}/artifacts/{art['id']}").json()
    verdict = detail["versions"][0]["validation"]["verifier"]
    assert verdict["verdict"] == "fail"
    assert "30 minutes" in verdict["issues"][0]


# --------------------------------------------------- autonomy levels
def test_autonomy_l1_holds_artifacts_for_approval(client):
    from app.artifacts import manager

    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    client.patch(f"/api/projects/{pid}", json={"autonomy_level": 1})
    assert client.patch(f"/api/projects/{pid}",
                        json={"autonomy_level": 9}).status_code == 422

    conn = client.app.state.conn
    detail = manager.write_artifact(conn, client.app.state.settings, pid,
                                    "req.md", "# x", created_by="skill")
    # L1 simulation: the runtime marks proposals; verify the endpoint flow
    conn.execute("UPDATE artifacts SET validation_status='proposed' WHERE id=?",
                 (detail["id"],))
    conn.execute(
        "INSERT INTO review_items (id, project_id, kind, payload, suggestion,"
        " status, created_at) VALUES ('rev_ap', ?, 'artifact_approval', ?,"
        " '{}', 'open', 't')", (pid, json.dumps({"artifact_id": detail["id"]})))
    conn.commit()

    res = client.post(f"/api/projects/{pid}/artifacts/{detail['id']}/approve")
    assert res.status_code == 200
    assert res.json()["validation_status"] == "unverified"
    assert client.get(f"/api/projects/{pid}/review").json()["items"] == []

    # approving a non-proposed artifact is a conflict
    res = client.post(f"/api/projects/{pid}/artifacts/{detail['id']}/approve")
    assert res.status_code == 409


# --------------------------------------------- token budget + kill switch
def test_daily_budget_pauses_runs(client, settings):
    conn = _seed_project(settings)
    spend.set_daily_budget(conn, 10)
    assert spend.budget_exceeded(conn) is False

    spend.record_usage(conn, 100, 0)
    assert spend.tokens_used_today(conn) == 100
    assert spend.budget_exceeded(conn) is True

    script = [resp(content="hi there")]
    req = HarnessRequest(
        conn=conn, settings=settings, client=FakeGateway(script),
        project_id="prj_1", session_id="s", user_message="hello",
        user_message_id="m1",
    )
    events = list(NativeHarness().run(req))
    assert events[-1][1]["status"] == "FAILED"

    run = conn.execute("SELECT error_code FROM agent_runs").fetchone()
    assert run["error_code"] == "daily_budget"

    # raising the budget lets the same project run again
    spend.set_daily_budget(conn, 1_000_000)
    req2 = HarnessRequest(
        conn=conn, settings=settings, client=FakeGateway([resp(content="ok")]),
        project_id="prj_1", session_id="s2", user_message="hello",
        user_message_id="m2",
    )
    assert list(NativeHarness().run(req2))[-1][1]["status"] == "SUCCEEDED"


def test_kill_switch_blocks_runs_until_resumed(client, settings):
    conn = _seed_project(settings)
    spend.set_kill_switch(conn, True)

    req = HarnessRequest(
        conn=conn, settings=settings, client=FakeGateway([resp(content="x")]),
        project_id="prj_1", session_id="s", user_message="hello",
        user_message_id="m1",
    )
    events = list(NativeHarness().run(req))
    assert events[-1][1]["status"] == "FAILED"
    run = conn.execute("SELECT error_code FROM agent_runs").fetchone()
    assert run["error_code"] == "kill_switch"

    spend.set_kill_switch(conn, False)
    req2 = HarnessRequest(
        conn=conn, settings=settings, client=FakeGateway([resp(content="ok")]),
        project_id="prj_1", session_id="s2", user_message="hello",
        user_message_id="m2",
    )
    assert list(NativeHarness().run(req2))[-1][1]["status"] == "SUCCEEDED"


def test_token_usage_recorded_on_run_and_daily_total(client, settings):
    conn = _seed_project(settings)
    used_before = spend.tokens_used_today(conn)

    script = [resp(content="an answer")]
    script[0].prompt_tokens = 120
    script[0].completion_tokens = 30
    req = HarnessRequest(
        conn=conn, settings=settings, client=FakeGateway(script),
        project_id="prj_1", session_id="s", user_message="hello",
        user_message_id="m1",
    )
    events = list(NativeHarness().run(req))
    final = events[-1][1]
    assert final["prompt_tokens"] == 120 and final["completion_tokens"] == 30

    run = conn.execute(
        "SELECT prompt_tokens, completion_tokens FROM agent_runs").fetchone()
    assert run["prompt_tokens"] == 120 and run["completion_tokens"] == 30
    assert spend.tokens_used_today(conn) == used_before + 150


# ------------------------------------------- fix-attempt cap + escalation
def test_validation_failures_escalate_after_three_attempts(client, settings):
    from app.skills import loader, runtime
    from app.agent.tools import ToolContext

    conn = _seed_project(settings)
    skill_dir = settings.data_dir / "skills" / "validator"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text(
        "id: validator\nname: Validator\nversion: 1.0.0\n"
        "tools: [write_artifact, run_validator]\n", encoding="utf-8")
    skill = loader.load_skill_from_dir(skill_dir, builtin=False)

    class WriteThenFailValidator:
        """Writes once, then keeps failing validation forever."""

        tool_calling_mode = "native"

        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools=None, max_output_tokens=None):
            self.calls += 1
            names = [m.name for m in messages if m.role == "tool"]
            if "write_artifact" not in names:
                return LLMResponse(content=None, tool_calls=[ToolCall(
                    id="w1", name="write_artifact", arguments=json.dumps({
                        "file_name": "req.md",
                        # cites a nonexistent chunk → deterministic validation fails
                        "content": "# Business Context\n\n## Goals\n\n## Requirements\n\n### REQ-001 — t\n\n- Statement: s [chk_nope].\n"}))],
                    finish_reason="tool_calls")
            validator_attempts = names.count("run_validator")
            if validator_attempts < 4:  # keeps retrying past the cap
                return LLMResponse(content=None, tool_calls=[ToolCall(
                    id=f"v{validator_attempts + 1}", name="run_validator",
                    arguments=json.dumps({"file_name": "req.md", "skill_id": "ba"}))],
                    finish_reason="tool_calls")
            return LLMResponse(content="given up", tool_calls=[], finish_reason="stop")

        def stream(self, messages, tools=None, max_output_tokens=None):
            return iter([{"response": self.complete(messages)}])

    gw = WriteThenFailValidator()
    req = HarnessRequest(
        conn=conn, settings=settings, client=gw, project_id="prj_1",
        session_id="s", user_message="write req", user_message_id="m",
    )
    ctx = ToolContext(conn, settings, "prj_1")
    events = []
    gen = runtime.execute_skill_gen(req, ctx, skill, "write req", "run_1")
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        result = stop.value

    assert result["escalated"] is True
    assert gw.calls >= 5  # write + 3 failing validations + final give-up

    review = conn.execute(
        "SELECT kind, payload FROM review_items WHERE kind='validation_escalation'"
    ).fetchone()
    assert review is not None
    payload = json.loads(review["payload"])
    assert payload["attempts"] == 3
    assert payload["file_name"] == "req.md"


def _messages_with_names(messages):
    return messages
