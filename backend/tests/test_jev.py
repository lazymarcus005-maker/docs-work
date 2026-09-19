"""Public API and HTTP boundaries for the optional TypeSafe Jev tool."""
from __future__ import annotations

import json

import httpx

from app.jev.client import JevDecisionClient, JevError
from app.jev.client import JevDecision
from app.jev.routing import skill_for_decision


KEY = "ts-secret-key-123"


def choice_response(choice="general_question", confidence=0.91):
    choices = (
        "business_analysis", "summarize_sources", "general_question",
        "needs_clarification",
    )
    probabilities = {item: 0.01 for item in choices}
    probabilities[choice] = confidence
    rest = (1 - confidence) / (len(choices) - 1)
    for item in choices:
        if item != choice:
            probabilities[item] = rest
    return {
        "model": "jev-1.13.0",
        "answers": {"intent": {
            "type": "choice", "choice": choice,
            "probabilities": probabilities, "confidence": confidence,
        }},
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }


def test_jev_configuration_is_disabled_without_a_key(client):
    response = client.get("/api/settings/internal-tools/jev")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "has_api_key": False,
        "ready": False,
        "model": "jev-1.13.0",
    }


def test_jev_cannot_be_enabled_without_a_key(client):
    response = client.put(
        "/api/settings/internal-tools/jev", json={"enabled": True}
    )

    assert response.status_code == 409
    assert client.get("/api/settings/internal-tools/jev").json()["enabled"] is False


def test_jev_key_is_write_only_and_can_enable_tool(client):
    response = client.put(
        "/api/settings/internal-tools/jev",
        json={"api_key": KEY, "enabled": True},
    )

    assert response.status_code == 200
    assert response.json() == {
        "enabled": True, "has_api_key": True, "ready": True,
        "model": "jev-1.13.0",
    }
    assert KEY not in response.text
    assert KEY not in client.get("/api/settings/internal-tools/jev").text


def test_jev_connection_test_sends_only_a_synthetic_request(client):
    client.put("/api/settings/internal-tools/jev", json={"api_key": KEY})
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=choice_response())

    client.app.state.jev_transport = httpx.MockTransport(handler)
    response = client.post("/api/settings/internal-tools/jev/test")

    assert response.status_code == 200
    assert response.json()["reachable"] is True
    assert response.json()["model"] == "jev-1.13.0"
    assert seen["url"] == "https://api.typesafe.ai/v1/systemone"
    assert seen["authorization"] == f"Bearer {KEY}"
    assert seen["body"]["model"] == "jev-1.13.0"
    assert seen["body"]["state"].startswith("Synthetic Jev connectivity check.")
    assert response.json().get("enabled") is None


def test_jev_connection_test_requires_a_configured_key(client):
    response = client.post("/api/settings/internal-tools/jev/test")

    assert response.status_code == 409


def test_clearing_jev_key_also_disables_the_tool(client):
    client.put(
        "/api/settings/internal-tools/jev",
        json={"api_key": KEY, "enabled": True},
    )

    response = client.put(
        "/api/settings/internal-tools/jev", json={"clear_api_key": True}
    )

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["has_api_key"] is False
    assert client.app.state.secrets.get("secret://internal-tools/jev") is None


def test_client_uses_typesafe_choice_contract_and_returns_confidence():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=choice_response("business_analysis", 0.97))

    with JevDecisionClient(KEY, transport=httpx.MockTransport(handler)) as jev:
        decision = jev.classify("Create requirements from these sources")

    assert decision.category == "business_analysis"
    assert decision.confidence == 0.97
    assert decision.model == "jev-1.13.0"
    assert decision.usage["input_tokens"] == 12
    assert seen["url"] == "https://api.typesafe.ai/v1/systemone"
    assert seen["authorization"] == f"Bearer {KEY}"
    assert seen["body"]["state"] == "Create requirements from these sources"
    assert seen["body"]["questions"]["intent"]["type"] == "choice"
    assert set(seen["body"]["questions"]["intent"]["criteria"]) == {
        "business_analysis", "summarize_sources", "general_question",
        "needs_clarification",
    }


def test_client_rejects_unrecognized_choice_without_exposing_response_body():
    def handler(_request: httpx.Request) -> httpx.Response:
        body = choice_response()
        body["answers"]["intent"]["choice"] = "run_shell_command"
        return httpx.Response(200, json=body)

    with JevDecisionClient(KEY, transport=httpx.MockTransport(handler)) as jev:
        try:
            jev.classify("hello")
        except JevError as exc:
            assert "run_shell_command" not in str(exc)
        else:
            raise AssertionError("invalid Jev category was accepted")


def test_client_rejects_a_response_from_a_different_model_version():
    def handler(_request: httpx.Request) -> httpx.Response:
        body = choice_response()
        body["model"] = "jev-latest"
        return httpx.Response(200, json=body)

    with JevDecisionClient(KEY, transport=httpx.MockTransport(handler)) as jev:
        try:
            jev.classify("hello")
        except JevError as exc:
            assert "jev-latest" not in str(exc)
        else:
            raise AssertionError("an unpinned model response was accepted")


def test_client_turns_provider_errors_into_sanitized_errors():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": f"invalid key {KEY}"})

    with JevDecisionClient(KEY, transport=httpx.MockTransport(handler)) as jev:
        try:
            jev.classify("hello")
        except JevError as exc:
            assert exc.status_code == 401
            assert KEY not in str(exc)
        else:
            raise AssertionError("provider error was ignored")


def test_client_retries_type_safe_rate_limits_with_backoff():
    attempts = []
    delays = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json=choice_response())

    with JevDecisionClient(
        KEY, transport=httpx.MockTransport(handler), sleep=delays.append,
    ) as jev:
        decision = jev.classify("hello")

    assert decision.category == "general_question"
    assert len(attempts) == 2
    assert delays == [0.0]


def _chat_setup(client):
    project = client.post("/api/projects", json={"name": "Jev routing"}).json()
    client.post("/api/settings/llm-profiles", json={
        "name": "gateway", "base_url": "http://gateway.test/v1",
        "model": "cowork", "api_key": "gateway-secret",
    })
    return project["id"]


def _openai_stream(text):
    blocks = "".join(
        "data: " + json.dumps({"choices": [{"delta": {"content": part}}]}) + "\n\n"
        for part in (text,)
    ) + "data: [DONE]\n\n"
    return httpx.Response(
        200, content=blocks.encode(), headers={"content-type": "text/event-stream"}
    )


def _jev_transport(choice, confidence=0.99, counter=None):
    def handler(_request):
        if counter is not None:
            counter.append(1)
        return httpx.Response(200, json=choice_response(choice, confidence))
    return httpx.MockTransport(handler)


def test_disabled_chat_never_calls_jev(client):
    project_id = _chat_setup(client)
    calls = []
    client.app.state.jev_transport = _jev_transport(
        "business_analysis", counter=calls
    )
    client.app.state.llm_transport = httpx.MockTransport(
        lambda _request: _openai_stream("Normal chat response")
    )

    response = client.post(
        f"/api/projects/{project_id}/chat", json={"message": "hello"}
    )

    assert response.status_code == 200
    assert "Normal chat response" in response.text
    assert calls == []


def test_enabled_jev_routes_high_confidence_ba_request_inside_chat(client):
    project_id = _chat_setup(client)
    client.put(
        "/api/settings/internal-tools/jev",
        json={"api_key": KEY, "enabled": True},
    )
    jev_calls = []
    client.app.state.jev_transport = _jev_transport(
        "business_analysis", 0.99, jev_calls
    )

    def gateway_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        if "executing the 'Business Analyst' skill" in system:
            return _openai_stream("BA skill ran")
        return _openai_stream("BA result in the chat")

    client.app.state.llm_transport = httpx.MockTransport(gateway_handler)
    response = client.post(
        f"/api/projects/{project_id}/chat",
        json={"message": "Create requirements from the source files"},
    )

    assert response.status_code == 200
    assert "BA result in the chat" in response.text
    assert "skill.started" in response.text
    assert len(jev_calls) == 1
    run = client.app.state.conn.execute(
        "SELECT selected_skill FROM agent_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert run["selected_skill"] == "ba"
    event = client.app.state.conn.execute(
        "SELECT data FROM events WHERE type='agent.jev.decision' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    data = json.loads(event["data"])
    assert data["route"] == "ba"
    assert "state" not in data and "prompt" not in data


def test_explicit_skill_command_bypasses_jev(client):
    project_id = _chat_setup(client)
    client.put(
        "/api/settings/internal-tools/jev",
        json={"api_key": KEY, "enabled": True},
    )

    def fail_if_called(_request):
        raise AssertionError("explicit skill command called Jev")

    client.app.state.jev_transport = httpx.MockTransport(fail_if_called)

    def gateway_handler(request: httpx.Request) -> httpx.Response:
        system = json.loads(request.content)["messages"][0]["content"]
        if "executing the 'Business Analyst' skill" in system:
            return _openai_stream("BA skill ran")
        return _openai_stream("Explicit skill answer")

    client.app.state.llm_transport = httpx.MockTransport(gateway_handler)
    response = client.post(
        f"/api/projects/{project_id}/chat",
        json={"message": "/ba Create requirements"},
    )

    assert response.status_code == 200
    assert "Explicit skill answer" in response.text
    assert "skill.started" in response.text


def test_jev_failure_falls_back_to_existing_chat_flow(client):
    project_id = _chat_setup(client)
    client.put(
        "/api/settings/internal-tools/jev",
        json={"api_key": KEY, "enabled": True},
    )
    client.app.state.jev_transport = httpx.MockTransport(
        lambda _request: httpx.Response(503, json={"detail": KEY})
    )
    client.app.state.llm_transport = httpx.MockTransport(
        lambda _request: _openai_stream("Fallback response")
    )

    response = client.post(
        f"/api/projects/{project_id}/chat", json={"message": "hello"}
    )

    assert response.status_code == 200
    assert "Fallback response" in response.text
    assert KEY not in response.text


def test_jev_route_requires_high_confidence_and_enabled_skill(client):
    project_id = _chat_setup(client)
    conn = client.app.state.conn
    conn.commit()
    decision = JevDecision(
        category="business_analysis", confidence=0.94,
        probabilities={"business_analysis": 0.94}, model="jev-1.13.0",
    )

    assert skill_for_decision(conn, project_id, decision) is None

    decision = JevDecision(
        category="business_analysis", confidence=0.99,
        probabilities={"business_analysis": 0.99}, model="jev-1.13.0",
    )
    assert skill_for_decision(conn, project_id, decision) == "ba"

    conn.execute(
        "INSERT INTO project_skills (project_id, skill_id, enabled) VALUES (?, 'ba', 0)",
        (project_id,),
    )
    conn.commit()
    assert skill_for_decision(conn, project_id, decision) is None
