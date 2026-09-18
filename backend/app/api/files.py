"""Project file upload (ticket #3): validate, store, hash, list (spec §6.2,
§8, §39, FR-002). Parsing starts here in ticket #5."""
from __future__ import annotations

import hashlib
import sqlite3
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..config import Settings
from ..deps import get_app_settings, get_db
from ..jobs import queue
from ..storage import filesystem as fs
from ..util import log_event, new_id, now_iso

router = APIRouter(prefix="/api/projects/{project_id}/files", tags=["files"])

# spec §8 — V1 format support. Images are accepted but OCR-gated (ticket #15).
SUPPORTED: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    ".csv": "csv",
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
}


def get_document(conn: sqlite3.Connection, project_id: str, document_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM documents WHERE id = ? AND project_id = ?",
        (document_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    return dict(row)


def _unique_stored_name(context: Path, name: str) -> str:
    candidate = name
    stem, dot, suffix = name.partition(".")
    n = 2
    while (context / candidate).exists():
        candidate = f"{stem}-{n}{dot}{suffix}"
        n += 1
    return candidate


def _store_upload(settings: Settings, project_id: str, upload: UploadFile) -> dict:
    original = upload.filename or "file"
    safe_name = fs.sanitize_filename(original)
    suffix = ("." + safe_name.rsplit(".", 1)[-1].lower()) if "." in safe_name else ""
    kind = SUPPORTED.get(suffix)
    if kind is None:
        raise HTTPException(
            status_code=415,
            detail=f"{original}: unsupported file type. Supported: "
            + ", ".join(sorted(SUPPORTED)),
        )

    limit_bytes = settings.max_upload_mb * 1024 * 1024
    context = fs.context_dir(settings.workspace_root, project_id)
    context.mkdir(parents=True, exist_ok=True)
    stored_name = _unique_stored_name(context, safe_name)
    dest = fs.safe_join(context, stored_name)

    sha = hashlib.sha256()
    size = 0
    with open(dest, "wb") as out:
        while chunk := upload.file.read(1024 * 1024):
            size += len(chunk)
            if size > limit_bytes:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"{original}: exceeds the configured upload limit "
                    f"of {settings.max_upload_mb} MB",
                )
            sha.update(chunk)
            out.write(chunk)

    return {
        "stored_name": stored_name,
        "safe_name": safe_name,
        "kind": kind,
        "size": size,
        "sha256": sha.hexdigest(),
    }


@router.post("", status_code=201)
def upload_files(
    project_id: str,
    files: list[UploadFile],
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> dict:
    from .projects import require_project

    require_project(conn, project_id)
    if not files:
        raise HTTPException(status_code=422, detail="No files provided")

    out = []
    ts = now_iso()
    for upload in files:
        info = _store_upload(settings, project_id, upload)

        # incremental processing (§11, FR-004): same name + same hash →
        # reuse the existing result, no jobs enqueued
        existing = conn.execute(
            "SELECT id, status FROM documents WHERE project_id = ? AND name = ?",
            (project_id, info["safe_name"]),
        ).fetchone()
        if existing is not None:
            same = conn.execute(
                "SELECT content_hash FROM documents WHERE id = ?",
                (existing["id"],),
            ).fetchone()["content_hash"]
            if same == info["sha256"]:
                (fs.context_dir(settings.workspace_root, project_id)
                 / info["stored_name"]).unlink(missing_ok=True)  # drop duplicate copy
                out.append({"file_id": existing["id"], "name": info["safe_name"],
                            "status": existing["status"], "reused": True})
                continue

        fid = new_id("file")
        if existing is not None:
            # changed content under the same name: replace in place, reparse
            # only this source (Scenario G)
            fid = existing["id"]
            conn.execute(
                "UPDATE documents SET stored_name = ?, content_hash = ?,"
                " status = 'NEW', error = NULL, updated_at = ? WHERE id = ?",
                (info["stored_name"], info["sha256"], ts, fid),
            )
        else:
            conn.execute(
                "INSERT INTO documents (id, project_id, name, stored_name, media_type,"
                " kind, size_bytes, content_hash, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'NEW', ?, ?)",
                (
                    fid, project_id, info["safe_name"], info["stored_name"],
                    upload.content_type or "", info["kind"], info["size"],
                    info["sha256"], ts, ts,
                ),
            )
        conn.execute(
            "INSERT INTO document_versions (id, document_id, content_hash,"
            " stored_name, created_at) VALUES (?, ?, ?, ?, ?)",
            (new_id("docver"), fid, info["sha256"], info["stored_name"], ts),
        )
        out.append({"file_id": fid, "name": info["safe_name"], "status": "NEW"})
        log_event(conn, "file.uploaded", project_id, {"file": info["safe_name"]})
        queue.enqueue(
            conn, project_id, "PARSE_DOCUMENT", document_id=fid,
            priority=queue.PRIORITY_PARSE,
        )
    conn.commit()
    return {"files": out}


@router.get("")
def list_files(
    project_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    from .projects import require_project

    require_project(conn, project_id)
    rows = conn.execute(
        "SELECT id, name, kind, size_bytes, status, error, created_at,"
        " updated_at, last_processed_at FROM documents WHERE project_id = ?"
        " ORDER BY created_at",
        (project_id,),
    ).fetchall()
    return {"files": [dict(r) for r in rows]}


@router.get("/{document_id}/content")
def file_content(
    project_id: str,
    document_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> FileResponse:
    doc = get_document(conn, project_id, document_id)
    path = fs.safe_join(
        fs.context_dir(settings.workspace_root, project_id), doc["stored_name"]
    )
    if not path.exists():
        raise HTTPException(status_code=404, detail="Stored file missing on disk")
    return FileResponse(path, filename=doc["name"])


@router.post("/{document_id}/reprocess", status_code=202)
def reprocess_file(
    project_id: str,
    document_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Manual retry / reindex of a single source (§43, §57)."""
    get_document(conn, project_id, document_id)
    conn.execute(
        "UPDATE documents SET status = 'NEW', error = NULL, updated_at = ? WHERE id = ?",
        (now_iso(), document_id),
    )
    job = queue.enqueue(conn, project_id, "PARSE_DOCUMENT",
                        document_id=document_id, priority=queue.PRIORITY_PARSE)
    conn.commit()
    return {"file_id": document_id, "job_id": job}


@router.delete("/{document_id}", status_code=204)
def delete_file(
    project_id: str,
    document_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> None:
    doc = get_document(conn, project_id, document_id)
    from ..documents import retraction

    retraction.purge_document(conn, settings, project_id, doc)
    conn.commit()
    log_event(conn, "file.deleted", project_id, {"file": doc["name"]})
    conn.commit()
