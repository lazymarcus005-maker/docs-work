"""Ticket #7: agent harness loop, project tools, context manager."""
from __future__ import annotations

import json
import threading

import pytest

from app.agent import tools as tools_mod
from app.agent.context import build_context_pack, render_context_block
from app.agent.harness import HarnessRequest, NativeHarness
from app.config import Settings
from app.llm.base import ChatMessage, LLMResponse, ToolCall


# ------------------------------------------------------------ fake client
TS = "2026-01-01T00:00:00+00:00"


def named_projects_insert():
    return ("INSERT INTO projects (id, name, description, instruction, status,"
            " created_at, updated_at, autonomy_level)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)")


def _doc(conn, pid, name, kind="text", doc_id=None):
    doc_id = doc_id or f"doc_{name}"
    conn.execute(
        "INSERT INTO documents (id, project_id, name, stored_name, media_type,"
        " kind, size_bytes, content_hash, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, '', ?, 10, 'h', 'READY', ?, ?)",
        (doc_id, pid, name, name, kind, TS, TS),
    )
    return doc_id


def _chunk(conn, pid, doc_id, chunk_id, text, page=1):
    conn.execute(
        "INSERT INTO chunks (id, document_id, project_id, section_path, text,"
        " page, sequence, source_element_ids, created_at)"
        " VALUES (?, ?, ?, '[]', ?, ?, 1, '[]', ?)",
        (chunk_id, doc_id, pid, text, page, TS),
    )
    conn.execute("INSERT INTO chunks_fts (chunk_id, text, section_path) VALUES (?, ?, '[]')",
                 (chunk_id, text))


class FakeGateway:
    """Scripted LLM: pops a canned response per call, records requests."""

    def __init__(self, script: list[LLMResponse], tool_calling_mode: str = "native"):
        self.script = list(script)
        self.requests: list[list[ChatMessage]] = []
        self.tool_calling_mode = tool_calling_mode

    def complete(self, messages, tools=None, max_output_tokens=None):
        self.requests.append(list(messages))
        return self.script.pop(0)

    def stream(self, messages, tools=None, max_output_tokens=None):
        self.requests.append(list(messages))
        res = self.script.pop(0)
        if res.content and not res.has_tool_calls:
            for i in range(0, len(res.content), 7):
                yield {"delta": res.content[i:i + 7]}
        yield {"response": res}

    def health(self):
        return {"reachable": True}


def resp(content=None, tool_calls=None):
    return LLMResponse(content=content, tool_calls=tool_calls or [], finish_reason="stop")


def tool_call(name, args: dict, id="call_1"):
    return ToolCall(id=id, name=name, arguments=json.dumps(args))


# ------------------------------------------------------------------ tools
def test_tool_isolation_between_projects(settings):
    from app import db
    from app.agent.tools import ToolContext, execute

    conn = db.connect(settings.db_path)
    db.init_db(conn)
    ts = "2026-01-01"
    for pid in ("prj_a", "prj_b"):
        conn.execute("INSERT INTO projects (id, name, description, instruction, status, created_at, updated_at, autonomy_level) VALUES (?, ?, '', '', 'ACTIVE', ?, ?, 2)", (pid, pid, ts, ts))
    for pid, text in (("prj_a", "alpha secret sauce"), ("prj_b", "beta confidential data")):
        did = _doc(conn, pid, f"{pid}.txt")
        _chunk(conn, pid, did, f"chk_{pid}", text)
    conn.commit()

    ctx_a = ToolContext(conn, settings, "prj_a")
    assert "alpha" in json.dumps(execute(ctx_a, "search_project", {"query": "secret"}))
    assert "beta" not in json.dumps(execute(ctx_a, "search_project", {"query": "confidential"}))
    assert "error" in execute(ctx_a, "read_document", {"name": "prj_b.txt"})
    assert "error" in execute(ctx_a, "read_document", {"name": "nonexistent.txt"})

    # read_document returns traceable chunk ids
    out = execute(ctx_a, "read_document", {"name": "prj_a.txt"})
    assert "chk_prj_a" in out["content"]


# --------------------------------------------------------- context manager
def test_context_pack_budget_and_selection(settings):
    from app import db

    conn = db.connect(settings.db_path)
    db.init_db(conn)
    ts = "2026-01-01"
    pid = "prj_ctx"
    conn.execute(named_projects_insert(), (pid, "P", "", "", "ACTIVE", ts, ts, 2))
    _doc(conn, pid, "big.txt", doc_id="doc_1")
    _doc(conn, pid, "other.txt", doc_id="doc_2")
    for i in range(1, 8):
        _chunk(conn, pid, "doc_1", f"chk_{i:03}",
               f"authentication token flow number {i} " + "x" * 200)
    _chunk(conn, pid, "doc_2", "chk_other", "authentication in other doc")
    conn.commit()

    pack = build_context_pack(conn, settings, pid, "authentication token flow", budget_chars=900)
    assert pack["sources"], "pack must contain retrieved passages"
    assert pack["used_chars"] <= 900
    assert pack["truncated"]
    assert "authentic*" in render_context_block(pack) or "authentic" in render_context_block(pack)

    # selected files are prioritized in the pack
    pack_sel = build_context_pack(conn, settings, pid, "authentication", selected_files=["other.txt"], budget_chars=3000)
    assert pack_sel["sources"][0]["document"] == "other.txt"


# ---------------------------------------------------------------- harness
def _seed_project(settings, with_content=True):
    from app import db

    conn = db.connect(settings.db_path)
    db.init_db(conn)
    ts = "2026-01-01"
    conn.execute("INSERT INTO projects (id, name, description, instruction, status, created_at, updated_at, autonomy_level) VALUES (?, 'CX', 'Use evidence only.', 'instr', 'ACTIVE', ?, ?, 2)",
                 ("prj_1", ts, ts))
    if with_content:
        _doc(conn, "prj_1", "SRS.docx", kind="docx", doc_id="doc_1")
        _chunk(conn, "prj_1", "doc_1", "chk_0001",
               "The cxgateway forwards authentication requests to cxntlappux. "
               "Tokens expire after 30 minutes.", page=14)
    conn.commit()
    return conn


def _run_harness(settings, client_script, **kw):
    conn = kw.pop("conn") if "conn" in kw else _seed_project(settings)
    cancel = threading.Event()
    req = HarnessRequest(
        conn=conn, settings=settings, client=client_script,
        project_id="prj_1", session_id="ses_1",
        user_message=kw.pop("user_message", "What forwards authentication requests?"),
        user_message_id="msg_1",
        cancel_event=cancel,
        max_iterations=kw.pop("max_iterations", 6),
        max_tool_calls=kw.pop("max_tool_calls", 12),
    )
    events = list(NativeHarness().run(req))
    return events, conn


def test_harness_tool_loop_then_cited_answer(settings):
    fake = FakeGateway([
        resp(tool_calls=[tool_call("search_project", {"query": "authentication gateway"})]),
        resp(content="The cxgateway forwards authentication requests [chk_0001]."),
    ])
    events, conn = _run_harness(settings, fake)

    types = [e for e, _ in events]
    assert "context.search.completed" in types
    assert "tool.started" in types and "tool.completed" in types
    assert types[-1] == "run.completed"

    final = events[-1][1]
    assert final["status"] == "SUCCEEDED"
    assert final["content"] == "The cxgateway forwards authentication requests [chk_0001]."
    assert final["evidence_refs"] == ["chk_0001"]
    assert final["iterations"] == 2 and final["tool_calls"] == 1

    run = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (final["run_id"],)).fetchone()
    assert run["status"] == "SUCCEEDED"
    assert run["tool_call_count"] == 1 and run["iteration_count"] == 2

    # the tool observation was fed back as a role=tool message
    second_call = fake.requests[1]
    assert second_call[-1].role == "tool"
    assert "cxgateway" in second_call[-1].content


def test_harness_max_iterations_stop(settings):
    fake = FakeGateway([
        resp(tool_calls=[tool_call("search_project", {"query": "x"}, id=f"c{i}")])
        for i in range(10)
    ])
    events, conn = _run_harness(settings, fake, max_iterations=3)
    final = events[-1][1]
    assert final["status"] == "FAILED"
    run = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (final["run_id"],)).fetchone()
    assert run["status"] == "FAILED" and run["error_code"] == "max_iterations"


def test_harness_unknown_tool_becomes_observation_not_crash(settings):
    fake = FakeGateway([
        resp(tool_calls=[tool_call("delete_everything", {})]),
        resp(content="cannot do that"),
    ])
    events, conn = _run_harness(settings, fake)
    final = events[-1][1]
    assert final["status"] == "SUCCEEDED"
    tool_events = [d for t, d in events if t == "tool.completed"]
    assert tool_events and "unknown tool" in tool_events[0]["summary"]


def test_harness_cancel(settings):
    fake = FakeGateway([
        resp(tool_calls=[tool_call("search_project", {"query": "x"})]),
    ] * 20)
    conn = _seed_project(settings)
    cancel = threading.Event()
    cancel.set()  # cancelled before start
    req = HarnessRequest(
        conn=conn, settings=settings, client=fake, project_id="prj_1",
        session_id="ses_1", user_message="hi", user_message_id="m1",
        cancel_event=cancel, max_iterations=20,
    )
    events = list(NativeHarness().run(req))
    assert events[-1][1]["status"] == "CANCELLED"


def test_harness_ask_user_pauses_run(settings):
    fake = FakeGateway([
        resp(tool_calls=[tool_call("ask_user", {"question": "Which document should I use?"})]),
    ])
    events, conn = _run_harness(settings, fake)
    assert "run.waiting_user" in [t for t, _ in events]
    run = conn.execute("SELECT * FROM agent_runs").fetchone()
    assert run["status"] == "WAITING_USER"


def test_harness_prompt_json_fallback_mode(settings):
    fake = FakeGateway(
        script=[
            resp(content='{"action": {"tool": "search_project", "arguments": {"query": "authentication"}}}'),
            resp(content='{"answer": "The gateway forwards auth requests [chk_0001]."}'),
        ],
        tool_calling_mode="prompt-json",
    )
    events, conn = _run_harness(settings, fake)
    final = events[-1][1]
    assert final["status"] == "SUCCEEDED"
    assert "chk_0001" in final["evidence_refs"]
    assert fake.requests[0][0].role == "system"
    assert "JSON" in fake.requests[0][0].content


def test_harness_prompt_json_invalid_gets_one_retry(settings):
    fake = FakeGateway(
        script=[
            resp(content="not json at all"),
            resp(content='{"answer": "recovered answer"}'),
        ],
        tool_calling_mode="prompt-json",
    )
    events, conn = _run_harness(settings, fake)
    final = events[-1][1]
    assert final["status"] == "SUCCEEDED"
    assert final["content"] == "recovered answer"
    assert len(fake.requests) == 2


def test_prompt_json_unfixable_violation_fails_the_run(settings):
    fake = FakeGateway(
        script=[
            resp(content='{"banana": true}'),
            resp(content='{"action": {"tool": 42, "arguments": {}}}'),  # still invalid
        ],
        tool_calling_mode="prompt-json",
    )
    events, conn = _run_harness(settings, fake)
    final = events[-1][1]
    assert final["status"] == "FAILED"  # never executed an unvalidated action
