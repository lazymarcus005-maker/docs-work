"""Ticket #2: create, list, reopen, update, delete project workspaces."""
from __future__ import annotations

from app.storage import filesystem as fs


def test_create_project_persists_metadata_and_workspace(client, settings):
    res = client.post(
        "/api/projects",
        json={"name": "CXGateway Migration", "instruction": "Use uploaded evidence only."},
    )
    assert res.status_code == 201
    project = res.json()
    assert project["name"] == "CXGateway Migration"
    assert project["status"] == "ACTIVE"

    pdir = fs.project_dir(settings.workspace_root, project["id"])
    for sub in ("context", "parsed", "knowledge", "outputs", "sessions", "tmp", "logs"):
        assert (pdir / sub).is_dir()
    assert (pdir / "project.yaml").exists()


def test_create_project_requires_name(client):
    assert client.post("/api/projects", json={"name": "  "}).status_code == 422


def test_list_and_reopen_project(client):
    created = client.post("/api/projects", json={"name": "P1"}).json()
    listed = client.get("/api/projects").json()["projects"]
    assert [p["id"] for p in listed] == [created["id"]]

    reopened = client.get(f"/api/projects/{created['id']}")
    assert reopened.status_code == 200
    assert reopened.json()["name"] == "P1"
    assert reopened.json()["file_count"] == 0 and reopened.json()["ready_count"] == 0


def test_get_missing_project_404(client):
    assert client.get("/api/projects/prj_nope").status_code == 404


def test_update_project_instruction(client):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    res = client.patch(f"/api/projects/{pid}", json={"instruction": "Be precise."})
    assert res.json()["instruction"] == "Be precise."


def test_delete_project_removes_rows_and_directory(client, settings):
    pid = client.post("/api/projects", json={"name": "Doomed"}).json()["id"]
    assert client.delete(f"/api/projects/{pid}").status_code == 204
    assert client.get(f"/api/projects/{pid}").status_code == 404
    assert not fs.project_dir(settings.workspace_root, pid).exists()


def test_projects_survive_restart(client, settings):
    pid = client.post("/api/projects", json={"name": "Persistent"}).json()["id"]
    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings)) as fresh:
        assert fresh.get(f"/api/projects/{pid}").json()["name"] == "Persistent"
