"""Artifact validation, separate from generation (ticket #9, spec §46).

Validators are deterministic: required sections, unique requirement IDs,
and cited evidence existence. Skill manifests can declare a schema.json
for structural validation of structured outputs.
"""
from __future__ import annotations

import json
import re
import sqlite3

from ..config import Settings

REQ_ID_RE = re.compile(r"\b(REQ|NFR|FR|US)-\d+\b")
CHUNK_REF_RE = re.compile(r"\bchk_[A-Za-z0-9_]+\b")


def validate_artifact(
    conn: sqlite3.Connection,
    settings: Settings,
    project_id: str,
    file_name: str,
    skill_id: str | None = None,
) -> dict:
    from ..artifacts import manager

    rows = manager.list_artifacts(conn, project_id)
    match = next((a for a in rows if a["file_name"] == file_name), None)
    if match is None:
        return {"error": f"artifact not found: {file_name}"}
    version = manager.read_version(conn, project_id, match["id"])
    content = version["content"]

    checks: list[dict] = []

    # required sections declared by the skill (or a sensible default)
    required_sections = ["Requirements"]
    if skill_id:
        from .loader import get_skill

        skill = get_skill(conn, skill_id)
        if skill:
            schema_path = skill.source_dir / "schema.json"
            if schema_path.exists():
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                required_sections = schema.get("required_sections", required_sections)
    for section in required_sections:
        present = section.lower() in content.lower()
        checks.append({"check": f"required section: {section}", "passed": present})

    # unique requirement IDs
    ids = REQ_ID_RE.findall(content)
    dupes = {i for i in ids if ids.count(i) > 1}
    checks.append({
        "check": "requirement IDs unique",
        "passed": not dupes,
        "detail": sorted(dupes) if dupes else None,
    })

    # cited evidence exists in the project
    cited = sorted(set(CHUNK_REF_RE.findall(content)))
    existing = set()
    for cid in cited:
        row = conn.execute(
            "SELECT 1 FROM chunks WHERE id = ? AND project_id = ?", (cid, project_id)
        ).fetchone()
        if row:
            existing.add(cid)
    missing = [c for c in cited if c not in existing]
    checks.append({
        "check": "cited evidence exists",
        "passed": not missing,
        "detail": missing or None,
    })

    passed = all(c["passed"] for c in checks)
    result = {
        "artifact": file_name,
        "validation_status": "passed" if passed else "failed",
        "checks": checks,
    }
    conn.execute(
        "UPDATE artifacts SET validation_status = ? WHERE id = ?",
        (result["validation_status"], match["id"]),
    )
    conn.execute(
        "UPDATE artifact_versions SET validation = ? WHERE artifact_id = ? AND version = ?",
        (json.dumps(result), match["id"], version["version"]),
    )
    conn.commit()
    return result
