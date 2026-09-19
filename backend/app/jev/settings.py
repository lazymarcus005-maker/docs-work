"""Persistence helpers for the optional, app-wide Jev tool."""
from __future__ import annotations

import sqlite3

from ..secrets import SecretStore
from .client import MODEL_ID

ENABLED_KEY = "internal_tools.jev.enabled"
SECRET_REF = "secret://internal-tools/jev"


def is_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (ENABLED_KEY,)).fetchone()
    return bool(row and row["value"] == "true")


def has_api_key(secrets: SecretStore) -> bool:
    return bool(secrets.get(SECRET_REF))


def public_settings(conn: sqlite3.Connection, secrets: SecretStore) -> dict:
    configured = has_api_key(secrets)
    enabled = is_enabled(conn)
    return {
        "enabled": enabled,
        "has_api_key": configured,
        "ready": configured and enabled,
        "model": MODEL_ID,
    }


def set_enabled(conn: sqlite3.Connection, enabled: bool) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (ENABLED_KEY, "true" if enabled else "false"),
    )
    conn.commit()
