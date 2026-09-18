"""Agent service: one harness turn streamed as SSE (used by chat and by
explicit skill runs, spec §19, §22)."""
from __future__ import annotations

import json
import sqlite3
import threading

from fastapi import Request
from fastapi.responses import StreamingResponse

from ..config import Settings
from ..llm import profiles
from ..secrets import SecretStore
from .harness import HarnessRequest, NativeHarness

# run_id -> threading.Event checked between loop steps for cancellation
CANCEL_REGISTRY: dict[str, threading.Event] = {}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def stream_turn(
    request: Request | None,
    conn: sqlite3.Connection,
    settings: Settings,
    secrets: SecretStore,
    project_id: str,
    session_id: str,
    user_message_id: str,
    message: str,
    selected_files: list,
    skill_id: str | None,
    profile: dict | None,
) -> StreamingResponse:
    run_id = "run_pending"

    def generate():
        nonlocal run_id
        yield _sse("run.started", {
            "run_id": run_id, "session_id": session_id,
            "user_message_id": user_message_id,
        })
        if profile is None:
            yield _sse("run.failed", {
                "run_id": run_id,
                "message": "No LLM profile is configured, so the agent cannot run.",
                "actions": ["Open Settings and add an LLM profile (base URL, API key, model)"],
            })
            return

        cancel = threading.Event()
        CANCEL_REGISTRY[run_id] = cancel

        transport = getattr(request.app.state, "llm_transport", None) if request else None
        client = profiles.build_client(conn, secrets, profile, transport=transport)
        final_text, evidence_refs, run_status = "", [], "FAILED"
        try:
            req = HarnessRequest(
                conn=conn, settings=settings, client=client,
                project_id=project_id, session_id=session_id,
                user_message=message, user_message_id=user_message_id,
                selected_files=selected_files, skill_id=skill_id,
                cancel_event=cancel,
                max_iterations=settings.harness_max_iterations,
                max_tool_calls=settings.harness_max_tool_calls,
                timeout_seconds=settings.harness_run_timeout_seconds,
            )
            for event, data in NativeHarness().run(req):
                if event == "run.started":
                    run_id = data["run_id"]
                    CANCEL_REGISTRY[run_id] = cancel
                if event == "run.completed":
                    final_text = data.get("content", "")
                    evidence_refs = data.get("evidence_refs", [])
                    run_status = data.get("status", "SUCCEEDED")
                yield _sse(event, data)
        finally:
            CANCEL_REGISTRY.pop(run_id, None)
            client.close()
            if final_text:
                from ..util import new_id, now_iso

                conn.execute(
                    "INSERT INTO messages (id, session_id, project_id, role,"
                    " content, meta, created_at) VALUES (?, ?, ?, 'assistant', ?, ?, ?)",
                    (new_id("msg"), session_id, project_id, final_text,
                     json.dumps({"run_id": run_id, "evidence_refs": evidence_refs}),
                     now_iso()),
                )
                conn.commit()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def request_cancel(run_id: str) -> bool:
    event = CANCEL_REGISTRY.get(run_id)
    if event is None:
        return False
    event.set()
    return True
