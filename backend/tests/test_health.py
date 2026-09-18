"""Ticket #1: app shell tracer bullet — the API boots, database initializes,
health answers end-to-end."""
from __future__ import annotations


def test_health_reports_app_and_database(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["projects"] == 0
    assert body["version"]


def test_projects_page_empty_list(client):
    res = client.get("/api/projects")
    assert res.status_code == 200
    assert res.json() == {"projects": []}
