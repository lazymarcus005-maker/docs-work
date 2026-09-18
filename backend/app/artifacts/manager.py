"""Artifact manager (ticket #8, spec §23, FR-009).

Generated documents are managed artifacts: files in the project outputs
directory plus versioned records (v1, v2, …) with metadata. User edits
create new versions; nothing is discarded (Scenario E)."""
from __future__ import annotations

import json
import sqlite3

from ..config import Settings
from ..storage import filesystem as fs
from ..util import log_event, new_id, now_iso


class ArtifactError(Exception):
    pass


def write_artifact(
    conn: sqlite3.Connection,
    settings: Settings,
    project_id: str,
    file_name: str,
    content: str,
    skill_id: str | None = None,
    skill_version: str | None = None,
    source_context: list | None = None,
    created_by: str = "skill",
) -> dict:
    safe = fs.sanitize_filename(file_name)
    if not safe.endswith((".md", ".markdown", ".txt")):
        safe += ".md"
    ts = now_iso()
    existing = conn.execute(
        "SELECT * FROM artifacts WHERE project_id = ? AND file_name = ?",
        (project_id, safe),
    ).fetchone()
    if existing:
        return _new_version(
            conn, settings, dict(existing), content, source_context or [],
            created_by, skill_id, skill_version,
        )

    artifact_id = new_id("art")
    conn.execute(
        "INSERT INTO artifacts (id, project_id, file_name, type, skill_id,"
        " skill_version, current_version, created_at, updated_at)"
        " VALUES (?, ?, ?, 'markdown', ?, ?, 1, ?, ?)",
        (artifact_id, project_id, safe, skill_id, skill_version, ts, ts),
    )
    version = _insert_version(conn, artifact_id, 1, content, source_context or [], created_by)
    _write_file(settings, project_id, safe, content)
    log_event(conn, "artifact.created", project_id, {"artifact": safe, "version": 1})
    conn.commit()
    return _artifact_detail(conn, artifact_id)


def _new_version(
    conn: sqlite3.Connection, settings: Settings, artifact: dict, content: str,
    source_context: list, created_by: str, skill_id: str | None, skill_version: str | None,
) -> dict:
    artifact_id = artifact["id"]
    new_version = artifact["current_version"] + 1
    ts = now_iso()
    conn.execute(
        "UPDATE artifacts SET current_version = ?, updated_at = ?,"
        " skill_id = COALESCE(?, skill_id),"
        " skill_version = COALESCE(?, skill_version) WHERE id = ?",
        (new_version, ts, skill_id, skill_version, artifact_id),
    )
    _insert_version(conn, artifact_id, new_version, content, source_context, created_by)
    _write_file(settings, project_id=artifact["project_id"], file_name=artifact["file_name"], content=content)
    log_event(conn, "artifact.updated", artifact["project_id"],
              {"artifact": artifact["file_name"], "version": new_version})
    conn.commit()
    return _artifact_detail(conn, artifact_id)


def _insert_version(
    conn: sqlite3.Connection, artifact_id: str, version: int, content: str,
    source_context: list, created_by: str,
) -> None:
    conn.execute(
        "INSERT INTO artifact_versions (id, artifact_id, version, content,"
        " source_context, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (new_id("artv"), artifact_id, version, content,
         json.dumps(source_context, ensure_ascii=False), created_by, now_iso()),
    )


def _write_file(settings: Settings, project_id: str, file_name: str, content: str) -> None:
    outdir = fs.outputs_dir(settings.workspace_root, project_id)
    outdir.mkdir(parents=True, exist_ok=True)
    (fs.safe_join(outdir, file_name)).write_text(content, encoding="utf-8")


def list_artifacts(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, file_name, type, skill_id, validation_status,"
        " current_version, created_at, updated_at FROM artifacts"
        " WHERE project_id = ? ORDER BY updated_at DESC",
        (project_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_artifact(conn: sqlite3.Connection, project_id: str, artifact_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM artifacts WHERE id = ? AND project_id = ?",
        (artifact_id, project_id),
    ).fetchone()
    if row is None:
        raise ArtifactError("artifact not found")
    return _artifact_detail(conn, row["id"])


def _artifact_detail(conn: sqlite3.Connection, artifact_id: str) -> dict:
    row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    versions = conn.execute(
        "SELECT version, source_context, validation, created_by, created_at"
        " FROM artifact_versions WHERE artifact_id = ? ORDER BY version",
        (artifact_id,),
    ).fetchall()
    detail = dict(row)
    detail["versions"] = []
    for v in versions:
        item = dict(v)
        item["source_context"] = json.loads(item["source_context"] or "[]")
        item["validation"] = json.loads(item["validation"] or "{}")
        detail["versions"].append(item)
    return detail


def read_version(conn: sqlite3.Connection, project_id: str, artifact_id: str, version: int | None = None) -> dict:
    artifact = get_artifact(conn, project_id, artifact_id)
    v = version or artifact["current_version"]
    row = conn.execute(
        "SELECT * FROM artifact_versions WHERE artifact_id = ? AND version = ?",
        (artifact_id, v),
    ).fetchone()
    if row is None:
        raise ArtifactError(f"version {v} not found")
    return dict(row)


def delete_artifact(conn: sqlite3.Connection, settings: Settings, project_id: str, artifact_id: str) -> None:
    artifact = get_artifact(conn, project_id, artifact_id)
    conn.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
    conn.commit()
    (fs.outputs_dir(settings.workspace_root, project_id) / artifact["file_name"]).unlink(missing_ok=True)
    log_event(conn, "artifact.deleted", project_id, {"artifact": artifact["file_name"]})
    conn.commit()
