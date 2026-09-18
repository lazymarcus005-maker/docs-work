"""Small shared helpers: ids, timestamps, structured event log (spec §42)."""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

logger = logging.getLogger("cowork")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


SENSITIVE_KEYS = {"api_key", "authorization", "secret", "password", "token"}


def scrub(data):
    """Mask secret-looking values so they never reach logs or API responses."""
    if isinstance(data, dict):
        return {
            k: ("***" if any(s in k.lower() for s in SENSITIVE_KEYS) else scrub(v))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [scrub(v) for v in data]
    return data


def log_event(
    conn: sqlite3.Connection,
    type: str,
    project_id: str | None = None,
    data: dict | None = None,
    debug_payloads: bool = False,
) -> None:
    """Persist a structured event. Metadata-only by default: document content,
    prompts, and model responses are excluded unless debug_payloads is on."""
    data = dict(data or {})
    if not debug_payloads:
        for key in ("content", "prompt", "response", "chunks", "text"):
            if key in data:
                data[key] = f"<{len(str(data[key]))} chars>"
    data = scrub(data)
    conn.execute(
        "INSERT INTO events (ts, type, project_id, data) VALUES (?, ?, ?, ?)",
        (now_iso(), type, project_id, json.dumps(data)),
    )
    logger.info("event=%s project=%s %s", type, project_id, data)
