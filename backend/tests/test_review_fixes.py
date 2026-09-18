"""Review-fix regression tests: token-bounded context, oversized chunk packs,
interrupted-run recovery after restart."""
from __future__ import annotations

from app.agent.context import build_context_pack
from tests.test_harness import _doc, _chunk


def test_context_budget_is_token_based(settings):
    assert settings.context_token_budget == 12000
    conn = _doc_and_chunk(settings, "x" * 100)
    pack = build_context_pack(conn, settings, "prj_1", "x")
    assert pack["used_chars"] <= settings.context_token_budget * 4


def test_single_oversized_chunk_still_yields_a_pack(settings):
    conn = _doc_and_chunk(settings, "y" * 9999)
    pack = build_context_pack(conn, settings, "prj_1", "y", budget_chars=1000)
    assert pack["sources"], "at least one (truncated) source must be included"
    assert len(pack["sources"][0]["texts"][0]["text"]) <= 1100
    assert pack["evidence"]


def test_interrupted_runs_get_terminal_state_on_restart(settings):
    from app import db
    from app.main import create_app
    from fastapi.testclient import TestClient

    conn = db.connect(settings.db_path)
    db.init_db(conn)
    ts = "2026-01-01T00:00:00+00:00"
    conn.execute("INSERT INTO projects VALUES ('prj_r', 'R', '', '', 'ACTIVE', ?, ?)", (ts, ts))
    conn.execute(
        "INSERT INTO agent_runs (id, project_id, status, started_at, updated_at)"
        " VALUES ('run_zombie', 'prj_r', 'RUNNING', ?, ?)", (ts, ts))
    conn.execute(
        "INSERT INTO agent_runs (id, project_id, status, started_at, updated_at)"
        " VALUES ('run_waiting', 'prj_r', 'WAITING_USER', ?, ?)", (ts, ts))
    conn.commit()
    conn.close()

    with TestClient(create_app(settings)):
        pass

    conn = db.connect(settings.db_path)
    zombie = conn.execute("SELECT status, error_code FROM agent_runs WHERE id='run_zombie'").fetchone()
    waiting = conn.execute("SELECT status FROM agent_runs WHERE id='run_waiting'").fetchone()
    assert zombie["status"] == "FAILED" and zombie["error_code"] == "interrupted_by_restart"
    assert waiting["status"] == "WAITING_USER"  # legitimately persistent


def _doc_and_chunk(settings, text):
    from app import db

    conn = db.connect(settings.db_path)
    db.init_db(conn)
    conn.execute(
        "INSERT INTO projects VALUES ('prj_1', 'P', '', '', 'ACTIVE', 't', 't')")
    _doc(conn, "prj_1", "a.txt", doc_id="doc_1")
    _chunk(conn, "prj_1", "doc_1", "chk_0001", text)
    return conn
