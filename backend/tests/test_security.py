"""Ticket #18 hardening: security controls (§39), observability (§42),
secret hygiene (§32.1)."""
from __future__ import annotations

import io
import json

from app.agent.tools import ToolContext, execute
from tests.test_harness import _seed_project


def test_path_traversal_blocked_on_project_ids(settings):
    from app.storage.filesystem import project_dir, safe_join

    for bad in ("../evil", "prj/../..", "a/b", "..", "."):
        try:
            project_dir(settings.workspace_root, bad)
            raised = False
        except ValueError:
            raised = True
        assert raised, f"project id {bad!r} must be rejected"

    import pytest

    with pytest.raises(ValueError):
        safe_join(settings.workspace_root, "../../etc/passwd")


def test_uploaded_filenames_cannot_escape(settings, client):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    res = client.post(
        f"/api/projects/{pid}/files",
        files={"files": ("../../evil.txt", io.BytesIO(b"x"), "text/plain")},
    )
    assert res.status_code == 201
    name = res.json()["files"][0]["name"]
    assert ".." not in name and "/" not in name


def test_project_isolation_across_files_and_search(client):
    pid_a = client.post("/api/projects", json={"name": "A"}).json()["id"]
    pid_b = client.post("/api/projects", json={"name": "B"}).json()["id"]
    client.post(f"/api/projects/{pid_b}/files",
                files={"files": ("secret-b.txt", io.BytesIO(b"beta classified"), "text/plain")})

    # project A cannot read B's documents through any tool surface
    ctx = ToolContext(client.app.state.conn, client.app.state.settings, pid_a)
    assert "error" in execute(ctx, "read_document", {"name": "secret-b.txt"})
    found = execute(ctx, "search_project", {"query": "classified"})
    assert "beta" not in json.dumps(found)

    # API-level: file content of another project 404s
    files_b = client.get(f"/api/projects/{pid_b}/files").json()["files"]
    fid = files_b[0]["id"]
    assert client.get(f"/api/projects/{pid_a}/files/{fid}/content").status_code == 404


def test_api_keys_never_in_read_apis_or_events(client):
    res = client.post("/api/settings/llm-profiles", json={
        "name": "gw", "base_url": "http://gw/v1", "model": "m",
        "api_key": "sk-super-secret-value",
    })
    assert "sk-super-secret" not in res.text

    # events table scrubbing (§42: API keys must never be logged)
    from app.util import log_event

    conn = client.app.state.conn
    log_event(conn, "test.event", None, {"api_key": "sk-super-secret-value", "safe": "ok"})
    row = conn.execute("SELECT data FROM events WHERE type='test.event'").fetchone()
    assert "sk-super-secret" not in row["data"]
    assert "ok" in row["data"]


def test_log_event_masks_content_by_default(client, settings):
    from app.util import log_event

    conn = client.app.state.conn
    log_event(conn, "chat.request", "prj_x",
              {"content": "very confidential document text"})
    row = conn.execute(
        "SELECT data FROM events WHERE type='chat.request'").fetchone()
    assert "confidential" not in row["data"]
    assert "<31 chars>" in row["data"]  # metadata-only by default (§42)


def test_errors_are_actionable_not_generic(client):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    res = client.post(
        f"/api/projects/{pid}/files",
        files={"files": ("virus.exe", io.BytesIO(b"MZ"), "application/x-msdownload")},
    )
    detail = res.json()["detail"]
    assert "unsupported" in detail.lower()
    assert "Supported:" in detail  # tells the user what to do instead

    res404 = client.get("/api/projects/prj_missing")
    assert res404.status_code == 404
    assert res404.json()["detail"] == "Project not found"


def test_structured_events_cover_the_flow(client, settings):
    """Minimum event vocabulary of §42 lands in the events table."""
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    client.post(f"/api/projects/{pid}/files",
                files={"files": ("a.txt", io.BytesIO(b"hello gateway"), "text/plain")})

    import time

    deadline = time.time() + 10
    while time.time() < deadline:
        files = client.get(f"/api/projects/{pid}/files").json()["files"]
        if files[0]["status"] == "READY":
            break
        time.sleep(0.1)

    types = {r["type"] for r in client.app.state.conn.execute(
        "SELECT DISTINCT type FROM events").fetchall()}
    assert {"project.created", "file.uploaded", "document.parsed",
            "index.updated"} <= types
