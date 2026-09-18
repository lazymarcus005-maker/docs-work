"""AI memory (long-term, per project) — spec §26.

Chat history must never be the only memory mechanism. This module is the
durable one: discrete, evidence-referenced memory records that survive
across sessions, are injected into agent context under a budget, and are
fully manageable (view / edit / archive / delete) by the user.

Standards enforced here:
- every memory records its source (user or agent run) and evidence refs
- agent-written memories are deduplicated and length-capped
- nothing is silently destroyed: archive is the default soft-removal
- injection is bounded (latest N active memories), memories are context,
  never instructions (spec §27 precedence)
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Optional

from ..util import new_id, now_iso

MAX_CONTENT_CHARS = 600
KINDS = ("fact", "decision", "preference", "glossary", "todo")
SOURCES = ("user", "agent")
INJECTION_LIMIT = 20


class MemoryError(Exception):
    pass


def _normalize(content: str) -> str:
    return re.sub(r"\s+", " ", content).strip().lower()


def create_memory(
    conn: sqlite3.Connection,
    project_id: str,
    content: str,
    kind: str = "fact",
    source: str = "user",
    source_refs: Optional[dict] = None,
) -> dict:
    content = re.sub(r"\s+", " ", content or "").strip()
    if not content:
        raise MemoryError("memory content is required")
    if len(content) > MAX_CONTENT_CHARS:
        content = content[: MAX_CONTENT_CHARS - 1].rstrip() + "…"
    if kind not in KINDS:
        raise MemoryError(f"kind must be one of: {', '.join(KINDS)}")
    if source not in SOURCES:
        raise MemoryError(f"source must be one of: {', '.join(SOURCES)}")

    # exact-duplicate guard (case/whitespace-insensitive)
    rows = conn.execute(
        "SELECT id, content FROM memories WHERE project_id = ? AND status = 'active'",
        (project_id,),
    ).fetchall()
    for row in rows:
        if _normalize(row["content"]) == _normalize(content):
            return get_memory(conn, project_id, row["id"])

    mid = new_id("mem")
    ts = now_iso()
    conn.execute(
        "INSERT INTO memories (id, project_id, content, kind, source, status,"
        " source_refs, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
        (mid, project_id, content, kind, source,
         json.dumps(source_refs or {}, ensure_ascii=False), ts, ts),
    )
    conn.commit()
    return get_memory(conn, project_id, mid)


def get_memory(conn: sqlite3.Connection, project_id: str, memory_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND project_id = ?",
        (memory_id, project_id),
    ).fetchone()
    if row is None:
        raise MemoryError("memory not found")
    item = dict(row)
    item["source_refs"] = json.loads(item["source_refs"] or "{}")
    return item


def list_memories(
    conn: sqlite3.Connection, project_id: str, include_archived: bool = False
) -> list[dict]:
    sql = "SELECT * FROM memories WHERE project_id = ?"
    if not include_archived:
        sql += " AND status = 'active'"
    sql += " ORDER BY updated_at DESC"
    out = []
    for row in conn.execute(sql, (project_id,)).fetchall():
        item = dict(row)
        item["source_refs"] = json.loads(item["source_refs"] or "{}")
        out.append(item)
    return out


def update_memory(
    conn: sqlite3.Connection,
    project_id: str,
    memory_id: str,
    content: Optional[str] = None,
    kind: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    current = get_memory(conn, project_id, memory_id)
    fields = {}
    if content is not None and content.strip():
        c = re.sub(r"\s+", " ", content).strip()
        fields["content"] = c[: MAX_CONTENT_CHARS - 1].rstrip() + "…" if len(c) > MAX_CONTENT_CHARS else c
    if kind is not None:
        if kind not in KINDS:
            raise MemoryError(f"kind must be one of: {', '.join(KINDS)}")
        fields["kind"] = kind
    if status is not None:
        if status not in ("active", "archived"):
            raise MemoryError("status must be active or archived")
        fields["status"] = status
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE memories SET {sets}, updated_at = ? WHERE id = ?",
            (*fields.values(), now_iso(), memory_id),
        )
        conn.commit()
    return get_memory(conn, project_id, memory_id)


def delete_memory(conn: sqlite3.Connection, project_id: str, memory_id: str) -> None:
    get_memory(conn, project_id, memory_id)
    conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    conn.commit()


def load_active_memories(
    conn: sqlite3.Connection, project_id: str, limit: int = INJECTION_LIMIT
) -> list[dict]:
    rows = conn.execute(
        "SELECT id, content, kind FROM memories WHERE project_id = ?"
        " AND status = 'active' ORDER BY updated_at DESC LIMIT ?",
        (project_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def render_memory_block(conn: sqlite3.Connection, project_id: str) -> str:
    """Rendered into the agent's system prompt. Memories are context for the
    agent to respect — never instructions that override project rules."""
    memories = load_active_memories(conn, project_id)
    if not memories:
        return "(no durable project memories yet)"
    lines = [f"- [{m['kind']}] {m['content']}" for m in memories]
    return "\n".join(lines)
