"""Context compaction — standard agent-harness behavior (spec §18/§19.1):
model-aware budgets, summarize-old instead of fail, preserve evidence."""
from __future__ import annotations

import json

from app.agent.compaction import (compact_messages, context_usage,
                                  messages_tokens, should_compact,
                                  truncate_text, working_budget)
from app.agent.harness import HarnessRequest, NativeHarness
from app.llm.base import ChatMessage, LLMError, LLMResponse, ToolCall
from tests.test_harness import FakeGateway, _seed_project, resp, tool_call


def test_truncate_text_keeps_head_and_tail():
    text = "A" * 5000 + "MIDDLE-MATTER" + "B" * 5000
    out = truncate_text(text, 1000)
    assert len(out) < 1200
    assert out.startswith("A")
    assert out.rstrip().endswith("B")
    assert "chars truncated" in out
    assert truncate_text("short", 1000) == "short"


def test_working_budget_and_threshold_math():
    assert working_budget(128000, 8192) == 128000 - 8192 - 2000
    assert not should_compact(500, 1000, 0.7)
    assert should_compact(800, 1000, 0.7)
    assert not should_compact(9999, 0, 0.7)  # no window configured → never


def test_usage_prefers_observed_provider_tokens():
    msgs = [ChatMessage(role="user", content="x" * 400)]  # ~100 tokens estimated
    assert context_usage(msgs, observed_prompt_tokens=None) == 100
    assert context_usage(msgs, observed_prompt_tokens=9000) == 9000


class CompactGateway(FakeGateway):
    """Scripted stream responses; complete() serves summaries separately."""

    def __init__(self, script, summary="SUMMARY: user asked about tokens; cited chk_0001."):
        super().__init__(script)
        self.summary = summary
        self.summarize_calls = 0

    def complete(self, messages, tools=None, max_output_tokens=None):
        self.summarize_calls += 1
        return LLMResponse(content=self.summary, tool_calls=[], finish_reason="stop")


def test_compaction_triggers_and_preserves_system_recent_and_summary(settings):
    conn = _seed_project(settings)
    from tests.test_harness import _chunk

    _chunk(conn, "prj_1", "doc_1", "chk_big", "auth payload text " * 30000)  # ~500KB

    script = [
        resp(tool_calls=[tool_call("search_project", {"query": "auth"}, id=f"c{i}")])
        for i in range(6)
    ] + [resp(content="done with the analysis")]
    gw = CompactGateway(script)
    req = HarnessRequest(
        conn=conn, settings=settings, client=gw, project_id="prj_1",
        session_id="s1", user_message="analyze the auth flow", user_message_id="m1",
        context_window_tokens=12000, max_output_tokens=100,
        compact_threshold=0.5, keep_recent=2, observation_char_cap=200000,
    )
    events = list(NativeHarness().run(req))
    final = events[-1][1]
    assert final["status"] == "SUCCEEDED"

    compacted = [d for t, d in events if t == "context.compacted"]
    assert compacted, "compaction must have triggered"
    assert any(c["after_tokens"] < c["before_tokens"] for c in compacted)
    assert gw.summarize_calls >= 1

    # system prompt survives intact; the task anchor and a summary note are
    # both present in the final prompt
    last_req = gw.requests[-1]
    assert last_req[0].role == "system"
    summary_msgs = [m for m in last_req
                    if "Earlier conversation summary" in (m.content or "")]
    assert summary_msgs, "compacted summary message present in the final prompt"
    assert any(m.role == "user" and "analyze the auth flow" in (m.content or "")
               for m in last_req), "original task anchor preserved verbatim"


def test_compaction_summary_is_mechanical_when_summarizer_fails(settings):
    conn = _seed_project(settings)
    from tests.test_harness import _chunk

    _chunk(conn, "prj_1", "doc_1", "chk_big", "auth payload text " * 30000)
    script = [
        resp(tool_calls=[tool_call("search_project", {"query": "auth"}, id=f"c{i}")])
        for i in range(6)
    ] + [resp(content="done")]

    class FailingSummarizer(FakeGateway):
        def complete(self, messages, tools=None, max_output_tokens=None):
            raise LLMError("server", "summarizer down")

    gw = FailingSummarizer(script)
    req = HarnessRequest(
        conn=conn, settings=settings, client=gw, project_id="prj_1",
        session_id="s1", user_message="go", user_message_id="m1",
        context_window_tokens=12000, max_output_tokens=100,
        compact_threshold=0.5, keep_recent=2,
    )
    events = list(NativeHarness().run(req))
    assert events[-1][1]["status"] == "SUCCEEDED"  # run survives summarizer failure
    compacted = [d for t, d in events if t == "context.compacted"]
    assert compacted and any(c["fallback"] is True for c in compacted)


def test_oversized_tool_observation_is_truncated_in_history(settings):
    conn = _seed_project(settings)
    # a huge chunk so search_project returns a very large observation
    from tests.test_harness import _chunk as add_chunk

    add_chunk(conn, "prj_1", "doc_1", "chk_big", "payload " * 20000)  # ~140KB
    script = [
        resp(tool_calls=[tool_call("search_project", {"query": "payload"})]),
        resp(content="summarized the payload"),
    ]
    gw = FakeGateway(script)
    req = HarnessRequest(
        conn=conn, settings=settings, client=gw, project_id="prj_1",
        session_id="s1", user_message="find payload", user_message_id="m1",
        observation_char_cap=6000,
    )
    events = list(NativeHarness().run(req))
    assert events[-1][1]["status"] == "SUCCEEDED"
    tool_msgs = [m for m in gw.requests[1] if m.role == "tool"]
    assert tool_msgs and len(tool_msgs[0].content) <= 6500
    assert "chars truncated" in tool_msgs[0].content


def test_skill_runtime_truncates_observations(client, settings):
    from app.skills import loader, runtime
    from app.agent.tools import ToolContext
    from app.llm.base import ToolCall
    from tests.test_harness import _chunk

    conn = _seed_project(settings)
    _chunk(conn, "prj_1", "doc_1", "chk_huge", "data " * 20000)
    skill = loader.load_skill_from_dir(
        settings.builtin_skills_dir / "summarizer", builtin=True)

    seen_tool_messages: list[str] = []

    class AlwaysSearch:
        tool_calling_mode = "native"

        def complete(self, messages, tools=None, max_output_tokens=None):
            for m in messages:
                if m.role == "tool":
                    seen_tool_messages.append(m.content or "")
            return LLMResponse(content=None, tool_calls=[
                ToolCall(id="c1", name="search_project",
                         arguments=json.dumps({"query": "data"}))],
                finish_reason="tool_calls")

        def stream(self, messages, tools=None, max_output_tokens=None):
            return iter([{"response": self.complete(messages)}])

    req = HarnessRequest(
        conn=conn, settings=settings, client=AlwaysSearch(),
        project_id="prj_1", session_id="s", user_message="go", user_message_id="m",
        observation_char_cap=4000,
    )
    ctx = ToolContext(conn, settings, "prj_1")
    gen = runtime.execute_skill_gen(req, ctx, skill, "search", "run_1")
    try:
        while True:
            next(gen)
    except StopIteration:
        pass

    assert seen_tool_messages, "search observation must have entered the history"
    assert all(len(t) <= 4500 for t in seen_tool_messages)
    assert any("chars truncated" in t for t in seen_tool_messages)
