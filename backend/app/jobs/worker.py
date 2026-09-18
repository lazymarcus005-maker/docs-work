"""Background job workers. Populated in ticket #5; the app factory expects
start_workers/stop_workers to exist from day one."""
from __future__ import annotations


def start_workers(app) -> None:  # noqa: ANN001
    return None


def stop_workers(app) -> None:  # noqa: ANN001
    return None
