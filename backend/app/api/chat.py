"""Chat API (ticket #6): sessions CRUD + streaming chat (spec §25, §26, §36,
FR-012). Ticket #7 routes turns through the full agent harness."""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..api.projects import require_project
from ..deps import get_app_settings, get_db, get_secrets
from ..llm import profiles
from ..llm.base import ChatMessage, LLMError
from ..util import log_event, new_id, now_iso

router = APIRouter(prefix="/api/projects/{project_id}", tags=["chat"])

# run_id -> threading.Event, checked between stream deltas for cancellation
CANCEL_REGISTRY: dict[str, threading.Event] = {}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class SessionIn(BaseModel):
    title: Optional[str] = None


class SessionPatch(BaseModel):
    title: str


class ChatIn(BaseModel):
    session_id: Optional[str] = None
    message: str
    selected_files: Optional[list] = None
    skill_id: Optional[str] = None
    llm_profile_id: Optional[str] = None
    harness: str = "native"


# ------------------------------------------------------------- sessions
def get_session(conn: sqlite3.Connection, project_id: str, session_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ? AND project_id = ?",
        (session_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return dict(row)


@router.get("/sessions")
def list_sessions(project_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    require_project(conn, project_id)
    rows = conn.execute(
        "SELECT id, title, created_at, updated_at FROM sessions"
        " WHERE project_id = ? ORDER BY updated_at DESC",
        (project_id,),
    ).fetchall()
    return {"sessions": [dict(r) for r in rows]}


@router.post("/sessions", status_code=201)
def create_session(
    project_id: str,
    body: SessionIn,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    require_project(conn, project_id)
    sid = new_id("ses")
    ts = now_iso()
    conn.execute(
        "INSERT INTO sessions (id, project_id, title, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (sid, project_id, body.title or "New chat", ts, ts),
    )
    conn.commit()
    return get_session(conn, project_id, sid)


@router.patch("/sessions/{session_id}")
def rename_session(
    project_id: str,
    session_id: str,
    body: SessionPatch,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    get_session(conn, project_id, session_id)
    if not body.title.strip():
        raise HTTPException(status_code=422, detail="Title is required")
    conn.execute(
        "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
        (body.title.strip(), now_iso(), session_id),
    )
    conn.commit()
    return get_session(conn, project_id, session_id)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(
    project_id: str,
    session_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> None:
    get_session(conn, project_id, session_id)
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()


@router.get("/sessions/{session_id}/messages")
def list_messages(
    project_id: str,
    session_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    get_session(conn, project_id, session_id)
    rows = conn.execute(
        "SELECT id, role, content, meta, created_at FROM messages"
        " WHERE session_id = ? ORDER BY rowid",
        (session_id,),
    ).fetchall()
    messages = []
    for r in rows:
        m = dict(r)
        m["meta"] = json.loads(m["meta"] or "{}")
        messages.append(m)
    return {"messages": messages}


# ----------------------------------------------------------------- chat
@router.post("/chat")
def chat(
    project_id: str,
    body: ChatIn,
    conn: sqlite3.Connection = Depends(get_db),
    settings=Depends(get_app_settings),
    secrets=Depends(get_secrets),
    request: Request = None,
) -> StreamingResponse:
    require_project(conn, project_id)
    llm_transport = getattr(request.app.state, "llm_transport", None) if request else None
    if not body.message.strip():
        raise HTTPException(status_code=422, detail="Message is required")

    session_id = body.session_id or create_session(
        project_id, SessionIn(), conn
    )["id"]

    ts = now_iso()
    user_message_id = new_id("msg")
    meta = {
        "selected_files": body.selected_files or [],
        "skill_id": body.skill_id,
        "llm_profile_id": body.llm_profile_id,
        "harness": body.harness,
    }
    conn.execute(
        "INSERT INTO messages (id, session_id, project_id, role, content, meta,"
        " created_at) VALUES (?, ?, ?, 'user', ?, ?, ?)",
        (user_message_id, session_id, project_id, body.message, json.dumps(meta), ts),
    )
    conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?", (ts, session_id)
    )
    conn.commit()

    profile = (
        profiles.get_profile(conn, body.llm_profile_id) if body.llm_profile_id
        else profiles.default_profile(conn)
    )

    run_id = new_id("run")
    started = threading.Event()

    def generate():
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

        conn.execute(
            "INSERT INTO agent_runs (id, session_id, project_id, user_message_id,"
            " harness_type, llm_profile_id, selected_skill, status, started_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?)",
            (run_id, session_id, project_id, user_message_id, body.harness,
             profile["id"], body.skill_id, ts, ts),
        )
        conn.commit()
        log_event(conn, "agent.run.started", project_id,
                  {"run_id": run_id, "harness": body.harness})
        conn.commit()

        cancel = threading.Event()
        CANCEL_REGISTRY[run_id] = cancel
        history = _conversation_history(conn, session_id)
        full: list[str] = []
        try:
            client = profiles.build_client(conn, secrets, profile, transport=llm_transport)
            try:
                for chunk in client.stream(history):
                    if cancel.is_set():
                        raise _Cancelled()
                    if "delta" in chunk:
                        full.append(chunk["delta"])
                        yield _sse("assistant.delta", {"run_id": run_id, "delta": chunk["delta"]})
                    elif "response" in chunk:
                        pass  # final LLMResponse — content already streamed
            finally:
                client.close()

            content = "".join(full)
            _persist_assistant_message(conn, session_id, project_id, content, run_id)
            conn.execute(
                "UPDATE agent_runs SET status = 'SUCCEEDED', completed_at = ?,"
                " updated_at = ? WHERE id = ?",
                (now_iso(), now_iso(), run_id),
            )
            conn.commit()
            log_event(conn, "agent.run.completed", project_id, {"run_id": run_id})
            conn.commit()
            yield _sse("run.completed", {"run_id": run_id, "session_id": session_id})
        except _Cancelled:
            conn.execute(
                "UPDATE agent_runs SET status = 'CANCELLED', completed_at = ?,"
                " updated_at = ? WHERE id = ?",
                (now_iso(), now_iso(), run_id),
            )
            if full:
                _persist_assistant_message(
                    conn, session_id, project_id,
                    "".join(full) + "\n\n*[cancelled]*", run_id,
                )
            conn.commit()
            yield _sse("run.cancelled", {"run_id": run_id})
        except LLMError as e:
            conn.execute(
                "UPDATE agent_runs SET status = 'FAILED', error_code = ?,"
                " completed_at = ?, updated_at = ? WHERE id = ?",
                (e.category, now_iso(), now_iso(), run_id),
            )
            conn.commit()
            log_event(conn, "agent.run.failed", project_id,
                      {"run_id": run_id, "category": e.category})
            conn.commit()
            yield _sse("run.failed", {
                "run_id": run_id,
                "message": f"The LLM call failed ({e.category}): {e}",
                "actions": ["Check the LLM profile settings", "Test the connection in Settings"],
            })
        finally:
            CANCEL_REGISTRY.pop(run_id, None)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class _Cancelled(Exception):
    pass


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    project_id: str,
    run_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    require_project(conn, project_id)
    ev = CANCEL_REGISTRY.get(run_id)
    if ev is not None:
        ev.set()
        return {"run_id": run_id, "cancelled": True}
    row = conn.execute("SELECT id FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    conn.execute(
        "UPDATE agent_runs SET status = 'CANCELLED', completed_at = ?, updated_at = ?"
        " WHERE id = ?",
        (now_iso(), now_iso(), run_id),
    )
    conn.commit()
    return {"run_id": run_id, "cancelled": True}


def _conversation_history(conn: sqlite3.Connection, session_id: str) -> list[ChatMessage]:
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY rowid",
        (session_id,),
    ).fetchall()
    return [ChatMessage(role=r["role"], content=r["content"]) for r in rows]


def _persist_assistant_message(
    conn: sqlite3.Connection, session_id: str, project_id: str, content: str, run_id: str
) -> None:
    conn.execute(
        "INSERT INTO messages (id, session_id, project_id, role, content, meta,"
        " created_at) VALUES (?, ?, ?, 'assistant', ?, ?, ?)",
        (new_id("msg"), session_id, project_id, content,
         json.dumps({"run_id": run_id}), now_iso()),
    )
