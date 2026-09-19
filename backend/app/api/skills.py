"""Skills API (ticket #8, spec §36, §54): registry, project enablement,
explicit skill runs under the harness."""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import yaml

from ..agent import service, tools as tools_mod
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


class SkillCreateIn(BaseModel):
    id: str
    name: str
    description: str = ""
    prompt: str
    tools: List[str] = Field(default_factory=list)
    version: str = "1.0.0"
    enabled_for_project: bool = True


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


@router.get("/skills/tool-catalog")
def skill_tool_catalog() -> dict:
    """Tools a custom skill may be granted, with their project-scoped purpose."""
    return {"tools": [
        {"name": tool.name, "description": tool.description}
        for tool in tools_mod.get_tools()
    ]}


@router.post("/projects/{project_id}/skills", status_code=201)
def create_skill(
    project_id: str,
    body: SkillCreateIn,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict:
    """Install a custom skill in the local skill registry and enable it here."""
    require_project(conn, project_id)

    skill_id = body.id.strip()
    name = body.name.strip()
    description = body.description.strip()
    prompt = body.prompt.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", skill_id):
        raise HTTPException(
            status_code=422,
            detail="Skill ID must use lowercase letters, numbers, hyphens, or underscores.",
        )
    if not name or len(name) > 120:
        raise HTTPException(status_code=422, detail="Name is required (up to 120 characters).")
    if len(description) > 1000:
        raise HTTPException(status_code=422, detail="Description must be 1,000 characters or less.")
    if not prompt or len(prompt) > 50_000:
        raise HTTPException(status_code=422, detail="Instructions are required (up to 50,000 characters).")
    if not body.version.strip() or len(body.version.strip()) > 40:
        raise HTTPException(status_code=422, detail="Version is required (up to 40 characters).")

    available_tools = {tool.name for tool in tools_mod.get_tools()}
    selected_tools = sorted(set(body.tools))
    unknown_tools = sorted(set(selected_tools) - available_tools)
    if unknown_tools:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown tools: {', '.join(unknown_tools)}",
        )

    skill_dir = settings.data_dir / "skills" / skill_id
    if conn.execute("SELECT 1 FROM skills WHERE id = ?", (skill_id,)).fetchone():
        raise HTTPException(status_code=409, detail="A skill with this ID already exists.")
    if skill_dir.exists():
        raise HTTPException(status_code=409, detail="A skill directory with this ID already exists.")

    manifest = {
        "id": skill_id,
        "name": name,
        "version": body.version.strip(),
        "enabled_by_default": False,
        "description": description,
        "tools": selected_tools,
        "rules": [],
    }
    created_dir = False
    try:
        skill_dir.mkdir(parents=True, exist_ok=False)
        created_dir = True
        (skill_dir / "skill.yaml").write_text(
            yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        (skill_dir / "prompt.md").write_text(prompt + "\n", encoding="utf-8")
        loader.sync_registry(conn, settings)
        loader.set_skill_enabled(conn, project_id, skill_id, body.enabled_for_project)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="A skill directory with this ID already exists.")
    except Exception:
        if created_dir:
            conn.execute("DELETE FROM project_skills WHERE skill_id = ?", (skill_id,))
            conn.execute("DELETE FROM skills WHERE id = ? AND builtin = 0", (skill_id,))
            conn.commit()
            shutil.rmtree(skill_dir, ignore_errors=True)
        raise

    return next(
        skill for skill in loader.skills_for_project(conn, project_id)
        if skill["id"] == skill_id
    )


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
