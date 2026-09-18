"""Ticket #3: upload, validate, store, list project files."""
from __future__ import annotations

import io

from app.storage import filesystem as fs


def _upload(client, pid, name, content, mime="application/octet-stream"):
    return client.post(
        f"/api/projects/{pid}/files",
        files={"files": (name, io.BytesIO(content), mime)},
    )


def test_upload_stores_hashes_and_lists_files(client, settings):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    res = _upload(client, pid, "SRS.docx", b"PK fake docx")
    assert res.status_code == 201
    files = res.json()["files"]
    assert files[0]["status"] == "NEW"

    listed = client.get(f"/api/projects/{pid}/files").json()["files"]
    assert len(listed) == 1
    assert listed[0]["name"] == "SRS.docx"
    assert listed[0]["size_bytes"] == len(b"PK fake docx")

    stored = fs.context_dir(settings.workspace_root, pid) / "SRS.docx"
    assert stored.read_bytes() == b"PK fake docx"


def test_upload_after_creation_and_restart(client, settings):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    _upload(client, pid, "notes.txt", b"hello")
    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings)) as fresh:
        listed = fresh.get(f"/api/projects/{pid}/files").json()["files"]
        assert [f["name"] for f in listed] == ["notes.txt"]


def test_unsupported_type_rejected_with_actionable_error(client):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    res = _upload(client, pid, "evil.exe", b"MZ")
    assert res.status_code == 415
    assert "unsupported" in res.json()["detail"].lower()


def test_duplicate_names_get_unique_stored_names(client):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    _upload(client, pid, "a.txt", b"one")
    _upload(client, pid, "a.txt", b"two")
    names = [f["name"] for f in client.get(f"/api/projects/{pid}/files").json()["files"]]
    assert names.count("a.txt") == 2  # display name kept for both


def test_path_traversal_in_filename_is_sanitized(client, settings):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    res = _upload(client, pid, "../../etc/passwd.txt", b"x")
    assert res.status_code == 201
    name = res.json()["files"][0]["name"]
    assert "/" not in name and ".." not in name


def test_file_content_download(client):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    fid = _upload(client, pid, "a.txt", b"body").json()["files"][0]["file_id"]
    res = client.get(f"/api/projects/{pid}/files/{fid}/content")
    assert res.status_code == 200
    assert res.content == b"body"


def test_delete_file(client):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    fid = _upload(client, pid, "a.txt", b"body").json()["files"][0]["file_id"]
    assert client.delete(f"/api/projects/{pid}/files/{fid}").status_code == 204
    assert client.get(f"/api/projects/{pid}/files").json()["files"] == []
