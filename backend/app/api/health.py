"""Health endpoint (ticket #1)."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from ..config import APP_VERSION
from ..deps import get_db

router = APIRouter(prefix="/api")


@router.get("/health")
def health(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    projects = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
    return {
        "status": "ok",
        "version": APP_VERSION,
        "database": "ok",
        "projects": projects,
    }
