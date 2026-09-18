"""Ticket #4: OpenAI-compatible provider + LiteLLM Gateway compatibility.

Uses httpx.MockTransport so no real endpoint is needed.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.llm.base import ChatMessage, LLMError, ToolDef
from app.llm.openai_compatible import OpenAICompatibleProvider

GATEWAY = "http://llm-gateway.local:4000/v1"


def chat_response(content="ok", tool_calls=None, finish="stop"):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
        message["content"] = None
    return {
        "id": "chatcmpl-1",
        "model": "company-agent-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def make_provider(handler, **kw) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url=GATEWAY,
        model="company-agent-model",
        api_key="sk-virtual-key",
        retry_count=0,
        transport=httpx.MockTransport(handler),
        **kw,
    )


def test_complete_posts_gateway_alias_and_parses():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat_response("hello from gateway"))

    p = make_provider(handler)
    res = p.complete([ChatMessage(role="user", content="hi")])
    assert res.content == "hello from gateway"
    assert res.prompt_tokens == 10
    assert seen["url"] == f"{GATEWAY}/chat/completions"
    assert seen["auth"] == "Bearer sk-virtual-key"
    assert seen["body"]["model"] == "company-agent-model"  # alias passed through


def test_tool_call_round_trip():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["tools"][0]["function"]["name"] == "search_project"
        return httpx.Response(200, json=chat_response(tool_calls=[{
            "id": "call_1", "type": "function",
            "function": {"name": "search_project", "arguments": '{"query": "auth"}'},
        }], finish="tool_calls"))

    p = make_provider(handler)
    res = p.complete(
        [ChatMessage(role="user", content="find auth requirements")],
        tools=[ToolDef("search_project", "Search project", {"type": "object"})],
    )
    assert res.has_tool_calls
    assert res.tool_calls[0].name == "search_project"
    assert json.loads(res.tool_calls[0].arguments)["query"] == "auth"


def test_streaming_yields_deltas_then_final():
    sse = (
        'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request):
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})

    p = make_provider(handler)
    chunks = list(p.stream([ChatMessage(role="user", content="hi")]))
    assert "".join(c["delta"] for c in chunks if "delta" in c) == "Hello"
    assert chunks[-1]["response"].finish_reason == "stop"


def test_streaming_assembles_fragmented_tool_calls():
    sse = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_9","function":{"name":"search_project","arguments":"{\\"qu"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"oe\\": \\"auth\\"}"}}]}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request):
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})

    p = make_provider(handler)
    chunks = list(p.stream([ChatMessage(role="user", content="search")]))
    tc = chunks[-1]["response"].tool_calls[0]
    assert tc.id == "call_9" and tc.name == "search_project"
    assert json.loads(tc.arguments) == {"quoe": "auth"}  # fragments concatenated


def test_error_categories():
    def make(status):
        return make_provider(lambda req: httpx.Response(status, json={"error": {"message": "boom"}}))

    with pytest.raises(LLMError) as e:
        make(401).complete([ChatMessage(role="user", content="x")])
    assert e.value.category == "auth"

    with pytest.raises(LLMError) as e:
        make(429).complete([ChatMessage(role="user", content="x")])
    assert e.value.category == "rate_limit"

    with pytest.raises(LLMError) as e:
        make(500).complete([ChatMessage(role="user", content="x")])
    assert e.value.category == "server"

    def refused(request):
        raise httpx.ConnectError("no route")

    with pytest.raises(LLMError) as e:
        make_provider(refused).complete([ChatMessage(role="user", content="x")])
    assert e.value.category == "connection"


def test_retries_rate_limit_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json=chat_response("recovered"))

    p = OpenAICompatibleProvider(
        base_url=GATEWAY, model="m", retry_count=2, transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
    )
    assert p.complete([ChatMessage(role="user", content="x")]).content == "recovered"
    assert calls["n"] == 2


def test_health_reports_reachability_auth_and_model():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json=chat_response("ok"))

    report = make_provider(handler).health()
    assert report["reachable"] is True
    assert report["authentication"] == "valid"
    assert report["model_callable"] is True
    assert "latency_ms" in report


def test_health_reports_invalid_auth():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    report = make_provider(handler).health()
    assert report["authentication"] == "invalid"
    assert report["error_category"] == "auth"


def test_health_reports_unreachable():
    def handler(request):
        raise httpx.ConnectError("down")

    report = make_provider(handler).health()
    assert report["reachable"] is False
    assert report["error_category"] == "connection"
