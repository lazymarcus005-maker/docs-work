"""Background job workers (ticket #5, spec §37–38).

Worker threads claim jobs from the DB queue with their own connections.
Worker count is bounded by the configured CPU worker limits; the loop is
idles efficiently and stops cleanly on shutdown.
"""
from __future__ import annotations

import threading
import traceback

from .. import db
from ..config import Settings
from . import queue

_HANDLERS: dict[str, callable] = {}


def register_handler(job_type: str, fn) -> None:
    _HANDLERS[job_type] = fn


class WorkerPool:
    def __init__(self, app, worker_count: int) -> None:
        self.app = app
        self.settings: Settings = app.state.settings
        self.stop_event = threading.Event()
        self.threads = [
            threading.Thread(target=self._loop, args=(i,),
                             name=f"cowork-worker-{i}", daemon=True)
            for i in range(max(1, worker_count))
        ]

    def _loop(self, worker_idx: int) -> None:
        conn = db.connect(self.settings.db_path)
        try:
            while not self.stop_event.is_set():
                job = queue.claim_next(conn, f"w{worker_idx}")
                if job is None:
                    self.stop_event.wait(0.25)
                    continue
                handler = _HANDLERS.get(job["type"])
                if handler is None:
                    queue.finish_job(conn, job["id"], error=f"no handler for {job['type']}")
                    continue
                try:
                    handler(conn, self.settings, job)
                except Exception as e:  # noqa: BLE001 — jobs must never kill the worker
                    queue.finish_job(conn, job["id"], error=f"{e}")
                    traceback.print_exc()
                else:
                    queue.finish_job(conn, job["id"])
        finally:
            conn.close()

    def start(self) -> None:
        for t in self.threads:
            t.start()

    def stop(self) -> None:
        self.stop_event.set()
        for t in self.threads:
            t.join(timeout=5)


_pool: WorkerPool | None = None


def start_workers(app) -> None:
    global _pool
    settings: Settings = app.state.settings
    conn = app.state.conn

    from ..documents.pipeline import parse_document_task
    from ..knowledge.extraction import extract_entities_task

    register_handler("PARSE_DOCUMENT", parse_document_task)
    register_handler("EXTRACT_ENTITIES", extract_entities_task)

    # jobs left RUNNING by a previous crash resume from the queue (spec §44)
    queue.requeue_interrupted(conn)

    worker_count = max(
        settings.max_parse_workers, settings.max_embedding_workers, 1
    )
    _pool = WorkerPool(app, worker_count)
    app.state.worker_pool = _pool
    _pool.start()


def stop_workers(app) -> None:
    global _pool
    if _pool is not None:
        _pool.stop()
        _pool = None
