"""FastAPI dependency helpers."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from fastapi import Request

from . import db
from .config import Settings
from .secrets import SecretStore


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """Provide an isolated SQLite connection for each API request.

    The app-level connection is retained for startup and background worker
    coordination. Sharing it across FastAPI's threadpool requests can run
    concurrent cursors/transactions on one sqlite3 connection, so request
    handlers get a short-lived connection instead.
    """
    conn: sqlite3.Connection = db.connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_secrets(request: Request) -> SecretStore:
    return request.app.state.secrets
