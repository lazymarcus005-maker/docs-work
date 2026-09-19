"""Ticket #4: LLM profile settings API — write-only keys, masked reads,
test connection."""
from __future__ import annotations

import httpx

PROFILE = {
    "name": "company-gateway",
    "base_url": "http://llm-gateway.local:4000/v1",
    "model": "company-agent-model",
    "api_key": "sk-secret-abc123",
    "tool_calling_mode": "auto",
}


def test_crud_and_masking(client):
    res = client.post("/api/settings/llm-profiles", json=PROFILE)
    assert res.status_code == 201
    body = res.json()
    assert body["is_default"] is True  # first profile becomes default
    assert "api_key" not in body and "api_key_ref" not in body
    assert body["has_api_key"] is True

    listed = client.get("/api/settings/llm-profiles").json()["profiles"]
    assert len(listed) == 1 and listed[0]["has_api_key"]
    assert "sk-secret" not in res.text  # secret never in any response

    updated = client.put(
        f"/api/settings/llm-profiles/{body['id']}",
        json={**PROFILE, "name": "renamed", "timeout_seconds": 60},
    )
    assert updated.json()["name"] == "renamed"
    assert updated.json()["timeout_seconds"] == 60

    assert client.delete(f"/api/settings/llm-profiles/{body['id']}").status_code == 204
    assert client.get("/api/settings/llm-profiles").json()["profiles"] == []


def test_invalid_tool_mode_rejected(client):
    res = client.post(
        "/api/settings/llm-profiles", json={**PROFILE, "tool_calling_mode": "chaos"}
    )
    assert res.status_code == 422


def test_default_profile_switch(client):
    a = client.post("/api/settings/llm-profiles", json=PROFILE).json()
    b = client.post(
        "/api/settings/llm-profiles", json={**PROFILE, "name": "second", "is_default": True}
    ).json()
    assert b["is_default"] is True
    assert client.get(f"/api/settings/llm-profiles/{a['id']}").json()["is_default"] is False


def test_test_connection_reports_capabilities(client):
    pid = client.post("/api/settings/llm-profiles", json=PROFILE).json()["id"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
            },
        )

    app = client.app
    app.state.llm_transport = httpx.MockTransport(handler)
    report = client.post(f"/api/settings/llm-profiles/{pid}/test").json()
    assert report["reachable"] is True
    assert report["authentication"] == "valid"
    assert report["model_callable"] is True
    app.state.llm_transport = None


def test_opencode_go_sends_app_user_agent_and_stable_session_header(client):
    profile = client.post("/api/settings/llm-profiles", json={
        **PROFILE,
        "name": "OpenCode Go",
        "base_url": "https://opencode.ai/zen/go/v1",
        "model": "deepseek-v4-flash",
    }).json()
    project_id = client.post("/api/projects", json={"name": "OpenCode Go"}).json()["id"]
    session_id = client.post(
        f"/api/projects/{project_id}/sessions", json={"title": "OpenCode Go test"}
    ).json()["id"]
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = (
            'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            'data: [DONE]\n\n'
        )
        return httpx.Response(
            200, content=body.encode(),
            headers={"content-type": "text/event-stream"},
        )

    client.app.state.llm_transport = httpx.MockTransport(handler)
    for message in ("first turn", "second turn"):
        response = client.post(
            f"/api/projects/{project_id}/chat",
            json={"session_id": session_id, "llm_profile_id": profile["id"], "message": message},
        )
        assert response.status_code == 200
        assert "event: run.completed" in response.text

    assert len(requests) == 2
    assert all(r.headers["user-agent"] == "local-cowork-knowledge-workspace/0.1" for r in requests)
    assert all(r.headers["x-opencode-session"] == session_id for r in requests)


def test_opencode_go_connection_probe_includes_session_header(client):
    profile = client.post("/api/settings/llm-profiles", json={
        **PROFILE,
        "base_url": "https://opencode.ai/zen/go/v1",
        "model": "deepseek-v4-flash",
    }).json()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
        })

    client.app.state.llm_transport = httpx.MockTransport(handler)
    report = client.post(
        f"/api/settings/llm-profiles/{profile['id']}/test"
    ).json()

    assert report["model_callable"] is True
    assert len(seen) == 2
    assert all(r.headers["user-agent"] == "local-cowork-knowledge-workspace/0.1" for r in seen)
    assert all(r.headers["x-opencode-session"] == f"connection-test-{profile['id']}" for r in seen)


def test_key_survives_restart_and_still_masks(client, settings):
    pid = client.post("/api/settings/llm-profiles", json=PROFILE).json()["id"]

    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings)) as fresh:
        body = fresh.get(f"/api/settings/llm-profiles/{pid}").json()
        assert body["has_api_key"] is True
        assert "sk-secret" not in fresh.get("/api/settings/llm-profiles").text
