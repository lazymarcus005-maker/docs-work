"""Skill loader + registry (ticket #8, spec §21).

A Skill is a reusable task package: a directory with skill.yaml (manifest),
optional prompt.md, workflow.yaml, templates/ and validators/. Skills are
installed independently from the core app: built-ins ship under
app/builtin_skills, user-installed ones live in <data>/skills.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config import Settings
from ..util import now_iso

MANIFEST_ALIASES = ("skill.yaml", "skill.yml", "manifest.yaml")


@dataclass
class Skill:
    id: str
    name: str
    version: str
    builtin: bool
    enabled_by_default: bool
    description: str
    source_dir: Path
    tools: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    workflow: list[str] = field(default_factory=list)
    prompt: str = ""

    @property
    def templates_dir(self) -> Path:
        return self.source_dir / "templates"

    def template(self, name: str) -> str | None:
        path = self.templates_dir / name
        return path.read_text(encoding="utf-8") if path.exists() else None


def load_skill_from_dir(path: Path, builtin: bool) -> Skill:
    manifest_path = next(
        (path / m for m in MANIFEST_ALIASES if (path / m).exists()), None
    )
    if manifest_path is None:
        raise ValueError(f"skill dir {path} has no skill.yaml manifest")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    for key in ("id", "name", "version"):
        if not manifest.get(key):
            raise ValueError(f"skill manifest {manifest_path} missing {key}")

    prompt = ""
    prompt_path = path / "prompt.md"
    if prompt_path.exists():
        prompt = prompt_path.read_text(encoding="utf-8")
    workflow = []
    wf_path = path / "workflow.yaml"
    if wf_path.exists():
        wf = yaml.safe_load(wf_path.read_text(encoding="utf-8")) or {}
        workflow = wf.get("steps", [])

    return Skill(
        id=manifest["id"],
        name=manifest["name"],
        version=str(manifest["version"]),
        builtin=builtin,
        enabled_by_default=bool(manifest.get("enabled_by_default", False)),
        description=(manifest.get("description") or "").strip(),
        source_dir=path,
        tools=list(manifest.get("tools") or []),
        rules=list(manifest.get("rules") or []),
        workflow=workflow,
        prompt=prompt,
    )


def discover_skills(settings: Settings) -> list[Skill]:
    skills: dict[str, Skill] = {}
    roots = [(settings.builtin_skills_dir, True), (settings.data_dir / "skills", False)]
    for root, builtin in roots:
        if not root.exists():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            try:
                skill = load_skill_from_dir(child, builtin)
            except ValueError:
                continue  # unreadable skill dirs are skipped, not fatal
            skills[skill.id] = skill
    return list(skills.values())


def sync_registry(conn: sqlite3.Connection, settings: Settings) -> list[str]:
    """Sync the skills table with the filesystem; returns skill ids."""
    ids = []
    for skill in discover_skills(settings):
        ids.append(skill.id)
        conn.execute(
            "INSERT INTO skills (id, name, version, builtin, enabled_by_default,"
            " description, source_dir, manifest) VALUES (?, ?, ?, ?, ?, ?, ?, '{}')"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
            " version=excluded.version, description=excluded.description,"
            " source_dir=excluded.source_dir, builtin=excluded.builtin,"
            " enabled_by_default=excluded.enabled_by_default",
            (skill.id, skill.name, skill.version, int(skill.builtin),
             int(skill.enabled_by_default), skill.description, str(skill.source_dir)),
        )
    conn.commit()
    return ids


def get_skill(conn: sqlite3.Connection, skill_id: str) -> Skill | None:
    row = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
    if row is None:
        return None
    try:
        return load_skill_from_dir(Path(row["source_dir"]), bool(row["builtin"]))
    except ValueError:
        return None


def skills_for_project(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT s.id, s.name, s.version, s.builtin, s.description, s.enabled_by_default,"
        " COALESCE(ps.enabled, s.enabled_by_default) AS enabled"
        " FROM skills s LEFT JOIN project_skills ps"
        " ON ps.project_id = ? AND ps.skill_id = s.id"
        " ORDER BY s.builtin DESC, s.id",
        (project_id,),
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["enabled"] = bool(item["enabled"])
        out.append(item)
    return out


def set_skill_enabled(
    conn: sqlite3.Connection, project_id: str, skill_id: str, enabled: bool
) -> None:
    conn.execute(
        "INSERT INTO project_skills (project_id, skill_id, enabled) VALUES (?, ?, ?)"
        " ON CONFLICT(project_id, skill_id) DO UPDATE SET enabled=excluded.enabled",
        (project_id, skill_id, int(enabled)),
    )
    conn.commit()
