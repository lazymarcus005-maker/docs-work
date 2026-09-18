"""FastAPI dependency helpers."""
from __future__ import annotations

import sqlite3
from fastapi import Request

from .config import Settings
from .secrets import SecretStore


def get_db(request: Request) -> sqlite3.Connection:
    return request.app.state.conn


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_secrets(request: Request) -> SecretStore:
    return request.app.state.secrets
