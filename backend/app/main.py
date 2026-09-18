"""Application factory (ticket #1).

Routers are registered here as tickets land: projects, files, settings,
search, chat, skills, artifacts, jobs, knowledge, transfer.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import db
from .config import APP_VERSION, Settings, get_settings
from .secrets import SecretStore


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_dirs()

    conn = db.connect(settings.db_path)
    db.init_db(conn)
    secrets = SecretStore(settings.secrets_path, settings.secret_key_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from .jobs.worker import start_workers, stop_workers

        start_workers(app)
        yield
        stop_workers(app)

    app = FastAPI(
        title="Local Cowork Knowledge Workspace",
        version=APP_VERSION,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.conn = conn
    app.state.secrets = secrets

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from .api import files, health, projects, settings

    app.include_router(health.router)
    app.include_router(projects.router)
    app.include_router(files.router)
    app.include_router(settings.router)

    return app
