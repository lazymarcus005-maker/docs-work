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
    from ..agent.adapters import HARNESS_TYPES

    if (body.harness or "native") not in HARNESS_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"harness must be one of: {', '.join(HARNESS_TYPES)}")
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
    session_title = conn.execute(
        "SELECT title FROM sessions WHERE id = ?", (session_id,),
    ).fetchone()
    if session_title and session_title["title"].strip().lower() in ("new chat", "new task"):
        title = body.message.strip().splitlines()[0]
        if title.startswith("/"):
            _command, _space, rest = title.partition(" ")
            title = rest.strip() or " ".join(body.message.strip().splitlines()[1:])
        title = " ".join(title.split())[:64] or "Project work"
        conn.execute(
            "UPDATE sessions SET title = ? WHERE id = ?", (title, session_id),
        )
    conn.commit()

    profile = (
        profiles.get_profile(conn, body.llm_profile_id) if body.llm_profile_id
        else profiles.default_profile(conn)
    )

    from ..agent import service

    return service.stream_turn(
        request, conn, settings, secrets, project_id, session_id,
        user_message_id, body.message, body.selected_files or [],
        body.skill_id, profile, harness_type=body.harness or "native",
    )


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    project_id: str,
    run_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    require_project(conn, project_id)
    from ..agent import service

    if service.request_cancel(run_id):
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
    conn: sqlite3.Connection, session_id: str, project_id: str, content: str,
    meta: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO messages (id, session_id, project_id, role, content, meta,"
        " created_at) VALUES (?, ?, ?, 'assistant', ?, ?, ?)",
        (new_id("msg"), session_id, project_id, content,
         json.dumps(meta or {"run_id": None}), now_iso()),
    )
    conn.commit()
