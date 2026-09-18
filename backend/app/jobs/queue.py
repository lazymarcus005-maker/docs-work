"""DB-backed job queue (ticket #5, spec §37).

Expensive processing runs through jobs persisted in the local database —
no Redis/RabbitMQ. Priority ordering implements the spec's §38 policy:
interactive chat (10) > user-triggered skills (20) > parsing (50) >
graph enrichment (70) > background reindex (90).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..util import new_id, now_iso

PRIORITY_CHAT = 10
PRIORITY_SKILL = 20
PRIORITY_PARSE = 50
PRIORITY_ENRICHMENT = 70
PRIORITY_REINDEX = 90

JOB_TYPES = {
    "PARSE_DOCUMENT",
    "OCR_DOCUMENT",
    "CHUNK_DOCUMENT",
    "EMBED_DOCUMENT",
    "EXTRACT_ENTITIES",
    "EXTRACT_RELATIONS",
    "RESOLVE_ENTITIES",
    "BUILD_KNOWLEDGE",
    "RUN_SKILL",
    "VALIDATE_ARTIFACT",
}


def enqueue(
    conn: sqlite3.Connection,
    project_id: str,
    type: str,
    document_id: str | None = None,
    payload: dict | None = None,
    priority: int = PRIORITY_PARSE,
) -> str:
    job_id = new_id("job")
    ts = now_iso()
    conn.execute(
        "INSERT INTO processing_jobs (id, project_id, document_id, type, priority,"
        " status, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)",
        (job_id, project_id, document_id, type, priority, json.dumps(payload or {}), ts, ts),
    )
    conn.commit()
    return job_id


def claim_next(conn: sqlite3.Connection, worker_id: str) -> dict | None:
    """Atomically claim the highest-priority pending job."""
    ts = now_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM processing_jobs WHERE status = 'PENDING'"
            " ORDER BY priority ASC, created_at ASC LIMIT 1"
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE processing_jobs SET status = 'RUNNING', started_at = ?,"
            " updated_at = ?, attempts = attempts + 1 WHERE id = ?",
            (ts, ts, row["id"]),
        )
        conn.execute("COMMIT")
        job = dict(row)
        job["status"] = "RUNNING"
        job["payload"] = json.loads(job["payload"] or "{}")
        return job
    except Exception:
        conn.execute("ROLLBACK")
        raise


def finish_job(conn: sqlite3.Connection, job_id: str, error: str | None = None) -> None:
    status = "FAILED" if error else "SUCCEEDED"
    conn.execute(
        "UPDATE processing_jobs SET status = ?, error = ?, finished_at = ?,"
        " updated_at = ? WHERE id = ?",
        (status, error, now_iso(), now_iso(), job_id),
    )
    conn.commit()


def requeue_interrupted(conn: sqlite3.Connection) -> int:
    """Jobs left RUNNING by a crash/restart go back to PENDING (spec §44)."""
    cur = conn.execute(
        "UPDATE processing_jobs SET status = 'PENDING', updated_at = ?"
        " WHERE status = 'RUNNING'",
        (now_iso(),),
    )
    conn.commit()
    return cur.rowcount


def list_jobs(conn: sqlite3.Connection, project_id: str, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT id, document_id, type, priority, status, error, created_at,"
        " updated_at, started_at, finished_at FROM processing_jobs"
        " WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
        (project_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_job(conn: sqlite3.Connection, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM processing_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    job["payload"] = json.loads(job["payload"] or "{}")
    return job
