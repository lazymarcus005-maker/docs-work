"""Ticket #6: chat sessions + streaming chat (spec §25–26, FR-012)."""
from __future__ import annotations

import httpx
import pytest


def _setup_project_with_profile(client, handler):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    profile = client.post("/api/settings/llm-profiles", json={
        "name": "gw", "base_url": "http://gw.local/v1", "model": "alias-1",
        "api_key": "sk-x",
    }).json()
    client.app.state.llm_transport = httpx.MockTransport(handler)
    return pid, profile


def _stream_events(client, pid, payload):
    res = client.post(f"/api/projects/{pid}/chat", json=payload)
    assert res.status_code == 200
    events = []
    for block in res.text.split("\n\n"):
        if not block.strip():
            continue
        lines = block.strip().split("\n")
        etype = next(l for l in lines if l.startswith("event: "))[len("event: "):]
        data = next(l for l in lines if l.startswith("data: "))[len("data: "):]
        import json

        events.append((etype, json.loads(data)))
    return events


def sse_response(text):
    chunks = [text[i:i + 3] for i in range(0, len(text), 3)]
    body = "".join(
        f'data: {{"choices":[{{"delta":{{"content":"{c}"}}}}]}}\n\n' for c in chunks
    ) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body.encode(),
                          headers={"content-type": "text/event-stream"})


def test_session_crud_and_persistence(client):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    ses = client.post(f"/api/projects/{pid}/sessions", json={"title": "Design chat"}).json()
    assert ses["title"] == "Design chat"

    renamed = client.patch(
        f"/api/projects/{pid}/sessions/{ses['id']}", json={"title": "Renamed"}
    ).json()
    assert renamed["title"] == "Renamed"

    listed = client.get(f"/api/projects/{pid}/sessions").json()["sessions"]
    assert [s["id"] for s in listed] == [ses["id"]]

    assert client.delete(f"/api/projects/{pid}/sessions/{ses['id']}").status_code == 204
    assert client.get(f"/api/projects/{pid}/sessions").json()["sessions"] == []


def test_chat_streams_deltas_and_persists_messages(client):
    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response("Project context is ready.")

    pid, _profile = _setup_project_with_profile(client, handler)
    events = _stream_events(client, pid, {"message": "hello agent"})
    types = [t for t, _ in events]
    assert types[0] == "run.started"
    assert "assistant.delta" in types
    assert types[-1] == "run.completed"

    text = "".join(d["delta"] for t, d in events if t == "assistant.delta")
    assert text == "Project context is ready."

    session_id = events[0][1]["session_id"]
    messages = client.get(
        f"/api/projects/{pid}/sessions/{session_id}/messages"
    ).json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "Project context is ready."

    # history is sent to the LLM on the next turn
    seen = {}
    def handler2(request):
        import json
        seen["messages"] = json.loads(request.content)["messages"]
        return sse_response("again")
    client.app.state.llm_transport = httpx.MockTransport(handler2)
    _stream_events(client, pid, {"session_id": session_id, "message": "more"})
    assert len(seen["messages"]) >= 4  # user, assistant, user


def test_chat_without_profile_fails_actionably(client):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    events = _stream_events(client, pid, {"message": "hi"})
    assert events[-1][0] == "run.failed"
    assert "profile" in events[-1][1]["message"].lower()
    assert events[-1][1]["actions"]


def test_chat_llm_error_streams_run_failed(client):
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    pid, _ = _setup_project_with_profile(client, handler)
    events = _stream_events(client, pid, {"message": "hi"})
    assert events[-1][0] == "run.failed"
    assert events[-1][1]["message"]


def test_chat_survives_restart(client, settings):
    def handler(request):
        return sse_response("kept")

    pid, _ = _setup_project_with_profile(client, handler)
    events = _stream_events(client, pid, {"message": "q"})
    session_id = events[0][1]["session_id"]

    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings)) as fresh:
        sessions = fresh.get(f"/api/projects/{pid}/sessions").json()["sessions"]
        assert sessions[0]["id"] == session_id
        messages = fresh.get(
            f"/api/projects/{pid}/sessions/{session_id}/messages"
        ).json()["messages"]
        assert messages[1]["content"] == "kept"
