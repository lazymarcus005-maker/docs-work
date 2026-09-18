"""Ticket #17: replaceable harness — Claude Agent SDK adapter slot (§19.2)."""
from __future__ import annotations

import httpx
import pytest

from app.agent.adapters import HARNESS_TYPES, get_harness
from app.agent.harness import NativeHarness


def test_native_is_the_default_harness():
    assert get_harness("native").harness_type == "native"
    assert get_harness("").harness_type == "native"
    assert get_harness(None).harness_type == "native"
    assert isinstance(get_harness("native"), NativeHarness)
    assert "native" in HARNESS_TYPES and "claude-agent-sdk" in HARNESS_TYPES


def test_unknown_harness_rejected(client):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    res = client.post(f"/api/projects/{pid}/chat",
                      json={"message": "hi", "harness": "chaos"})
    assert res.status_code == 422


def test_claude_sdk_adapter_degrades_actionably_when_uninstalled(client):
    """Without the SDK installed, selecting the adapter yields an actionable
    run.failed — the project stays valid and native still works (§71)."""
    try:
        import claude_agent_sdk  # noqa: F401

        pytest.skip("SDK installed in this environment")
    except ImportError:
        pass

    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    client.post("/api/settings/llm-profiles", json={
        "name": "gw", "base_url": "http://gw.local/v1", "model": "m", "api_key": "k",
    })

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "x"},
                         "finish_reason": "stop"}]})

    client.app.state.llm_transport = httpx.MockTransport(handler)
    res = client.post(f"/api/projects/{pid}/chat",
                      json={"message": "hi", "harness": "claude-agent-sdk"})
    assert "claude-agent-sdk' package is not installed" in res.text
    assert "native harness" in res.text

    # switching back to native requires no migration — project data intact
    ok = client.post(f"/api/projects/{pid}/chat", json={"message": "hi again"})
    assert ok.status_code == 200
    assert client.get(f"/api/projects/{pid}").status_code == 200
