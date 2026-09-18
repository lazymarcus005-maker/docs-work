"""Project export/import (ticket #16, spec §59, NFR-003).

A project exports as project-package.zip: metadata, context files,
outputs, instructions, and skill references. Parsed/index data is omitted
by default — it is rebuildable derived data. Secrets are never included.
"""
from __future__ import annotations

import io
import json
import sqlite3
import zipfile

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response

from ..config import Settings
from ..deps import get_app_settings, get_db
from ..storage import filesystem as fs
from ..util import new_id, now_iso
from .projects import require_project

router = APIRouter(prefix="/api/projects", tags=["transfer"])

SAFE_ZIP_ROOTS = {"metadata.json", "context/", "outputs/"}


@router.get("/{project_id}/export")
def export_project(
    project_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> Response:
    project = require_project(conn, project_id)
    pdir = fs.project_dir(settings.workspace_root, project_id)

    documents = conn.execute(
        "SELECT id, name, stored_name, media_type, kind, size_bytes, content_hash"
        " FROM documents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    skills = conn.execute(
        "SELECT skill_id, enabled FROM project_skills WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    metadata = {
        "package_version": 1,
        "exported_at": now_iso(),
        "project": {
            "name": project["name"],
            "description": project["description"],
            "instruction": project["instruction"],
        },
        "documents": [dict(d) for d in documents],
        "skill_references": [dict(s) for s in skills],
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=1))
        for folder in ("context", "outputs"):
            base = pdir / folder
            if base.exists():
                for path in base.rglob("*"):
                    if path.is_file():
                        z.write(path, f"{folder}/{path.relative_to(base)}")
    payload = buf.getvalue()
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{project_id}-package.zip"'},
    )


@router.post("/import", status_code=201)
def import_project(
    file: UploadFile,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict:
    try:
        archive = zipfile.ZipFile(io.BytesIO(file.file.read()))
        metadata = json.loads(archive.read("metadata.json"))
    except Exception:
        raise HTTPException(status_code=422, detail="Not a valid project package")

    project = metadata.get("project") or {}
    if not project.get("name"):
        raise HTTPException(status_code=422, detail="Package has no project metadata")

    pid = new_id("prj")
    ts = now_iso()
    conn.execute(
        "INSERT INTO projects (id, name, description, instruction, status,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?)",
        (pid, project["name"], project.get("description", ""),
         project.get("instruction", ""), ts, ts),
    )
    fs.ensure_project_dirs(settings.workspace_root, pid)
    (fs.project_dir(settings.workspace_root, pid) / "project.yaml").write_text(
        f"id: {pid}\nname: {project['name']!r}\nimported: {ts}\n", encoding="utf-8"
    )

    from ..jobs import queue

    imported_outputs: list[str] = []
    for entry in archive.namelist():
        if entry.endswith("/") or "/" not in entry:
            continue
        root = entry.split("/", 1)[0]
        if root not in ("context", "outputs"):
            continue  # only authoritative assets are restored (§59)
        target = fs.safe_join(fs.project_dir(settings.workspace_root, pid), entry)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(entry))
        if root == "context":
            doc = next((d for d in metadata.get("documents", [])
                        if d.get("stored_name") == entry.split("/", 1)[1]), None)
            name = doc["name"] if doc else entry.split("/", 1)[1]
            conn.execute(
                "INSERT INTO documents (id, project_id, name, stored_name, media_type,"
                " kind, size_bytes, content_hash, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'NEW', ?, ?)",
                (new_id("file"), pid, fs.sanitize_filename(name),
                 entry.split("/", 1)[1], (doc or {}).get("media_type", ""),
                 (doc or {}).get("kind", "text"), (doc or {}).get("size_bytes", 0),
                 (doc or {}).get("content_hash", ""), ts, ts),
            )
            queue.enqueue(conn, pid, "PARSE_DOCUMENT", priority=queue.PRIORITY_PARSE)
        else:
            imported_outputs.append(entry.split("/", 1)[1])

    # outputs are first-class artifacts: register restored files (§23)
    for file_name in imported_outputs:
        from ..artifacts import manager

        content = (fs.outputs_dir(settings.workspace_root, pid) / file_name).read_text(
            encoding="utf-8", errors="replace")
        manager.write_artifact(conn, settings, pid, file_name, content,
                               created_by="import")

    for ref in metadata.get("skill_references", []):
        conn.execute(
            "INSERT OR REPLACE INTO project_skills (project_id, skill_id, enabled)"
            " VALUES (?, ?, ?)", (pid, ref.get("skill_id"), int(ref.get("enabled", 1))),
        )
    conn.commit()

    row = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    return dict(row)
