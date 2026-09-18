"""AI memory API: user-managed durable project memories (spec §26)."""
from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..api.projects import require_project
from ..deps import get_db
from ..knowledge import memory

router = APIRouter(prefix="/api/projects/{project_id}/memories", tags=["memories"])


class MemoryIn(BaseModel):
    content: str
    kind: str = "fact"


class MemoryPatch(BaseModel):
    content: Optional[str] = None
    kind: Optional[str] = None
    status: Optional[str] = None


def _detail(conn, project_id, memory_id):
    try:
        return memory.get_memory(conn, project_id, memory_id)
    except memory.MemoryError:
        raise HTTPException(status_code=404, detail="Memory not found")


@router.get("")
def list_memories(
    project_id: str,
    include_archived: bool = False,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    require_project(conn, project_id)
    return {"memories": memory.list_memories(conn, project_id, include_archived)}


@router.post("", status_code=201)
def create_memory(
    project_id: str,
    body: MemoryIn,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    require_project(conn, project_id)
    try:
        return memory.create_memory(conn, project_id, body.content, body.kind)
    except memory.MemoryError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.patch("/{memory_id}")
def update_memory(
    project_id: str,
    memory_id: str,
    body: MemoryPatch,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    require_project(conn, project_id)
    _detail(conn, project_id, memory_id)
    try:
        return memory.update_memory(
            conn, project_id, memory_id,
            content=body.content, kind=body.kind, status=body.status)
    except memory.MemoryError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.delete("/{memory_id}", status_code=204)
def delete_memory(
    project_id: str,
    memory_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> None:
    require_project(conn, project_id)
    _detail(conn, project_id, memory_id)
    memory.delete_memory(conn, project_id, memory_id)
