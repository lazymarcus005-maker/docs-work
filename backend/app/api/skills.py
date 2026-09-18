"""Skills API (ticket #8, spec §36, §54): registry, project enablement,
explicit skill runs under the harness."""
from __future__ import annotations

import json
import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..agent import service
from ..config import Settings
from ..deps import get_app_settings, get_db, get_secrets
from ..llm import profiles
from ..secrets import SecretStore
from ..skills import loader
from ..util import new_id, now_iso
from .projects import require_project

router = APIRouter(prefix="/api", tags=["skills"])


class SkillToggle(BaseModel):
    enabled: bool


class SkillRunIn(BaseModel):
    instruction: str
    selected_files: Optional[List[str]] = None
    session_id: Optional[str] = None
    llm_profile_id: Optional[str] = None


@router.get("/skills")
def list_all_skills(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    rows = conn.execute(
        "SELECT id, name, version, builtin, enabled_by_default, description"
        " FROM skills ORDER BY builtin DESC, id"
    ).fetchall()
    return {"skills": [dict(r) for r in rows]}


@router.get("/projects/{project_id}/skills")
def project_skills(
    project_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    require_project(conn, project_id)
    return {"skills": loader.skills_for_project(conn, project_id)}


@router.put("/projects/{project_id}/skills/{skill_id}/enabled")
def toggle_skill(
    project_id: str,
    skill_id: str,
    body: SkillToggle,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    require_project(conn, project_id)
    if loader.get_skill(conn, skill_id) is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    loader.set_skill_enabled(conn, project_id, skill_id, body.enabled)
    skills = loader.skills_for_project(conn, project_id)
    return next(s for s in skills if s["id"] == skill_id)


@router.post("/projects/{project_id}/skills/{skill_id}/run")
def run_skill(
    project_id: str,
    skill_id: str,
    body: SkillRunIn,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
    secrets: SecretStore = Depends(get_secrets),
    request: Request = None,
) -> StreamingResponse:
    require_project(conn, project_id)
    skill = loader.get_skill(conn, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    enabled = {s["id"]: s["enabled"] for s in loader.skills_for_project(conn, project_id)}
    if not enabled.get(skill_id, False):
        raise HTTPException(status_code=409, detail=f"Skill {skill_id} is not enabled for this project")
    if not body.instruction.strip():
        raise HTTPException(status_code=422, detail="Instruction is required")

    session_id = body.session_id or new_id("ses")
    if not body.session_id:
        ts = now_iso()
        conn.execute(
            "INSERT INTO sessions (id, project_id, title, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id, project_id, f"Skill: {skill.name}", ts, ts),
        )
    else:
        from .chat import get_session

        get_session(conn, project_id, session_id)

    user_message_id = new_id("msg")
    conn.execute(
        "INSERT INTO messages (id, session_id, project_id, role, content, meta,"
        " created_at) VALUES (?, ?, ?, 'user', ?, ?, ?)",
        (user_message_id, session_id, project_id, body.instruction,
         json.dumps({"skill_id": skill_id, "explicit_run": True}), now_iso()),
    )
    conn.commit()

    profile = (
        profiles.get_profile(conn, body.llm_profile_id) if body.llm_profile_id
        else profiles.default_profile(conn)
    )
    return service.stream_turn(
        request, conn, settings, secrets, project_id, session_id,
        user_message_id, body.instruction, body.selected_files or [],
        skill_id, profile,
    )
