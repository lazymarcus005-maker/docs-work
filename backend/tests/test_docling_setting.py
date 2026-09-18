"""Docling parser as a user-controlled setting (spec §10.1, §11):
readiness shown in Settings, enable/disable toggles, stale-on-switch."""
from __future__ import annotations

import io
import time

from app.documents import parser as parser_mod
from app.documents.pipeline import mark_stale


def _make_docx(paragraphs):
    import docx as docx_lib
    from io import BytesIO

    d = docx_lib.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    buf = BytesIO()
    d.save(buf)
    return buf.getvalue()


def _upload_docx(client, pid, content=None):
    res = client.post(
        f"/api/projects/{pid}/files",
        files={"files": ("a.docx", io.BytesIO(_make_docx(content or ["gateway auth content"])),
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    return res.json()["files"][0]["file_id"]


def _wait_ready(client, pid, fid, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        files = client.get(f"/api/projects/{pid}/files").json()["files"]
        doc = next(f for f in files if f["id"] == fid)
        if doc["status"] == "READY":
            row = client.app.state.conn.execute(
                "SELECT parser_version FROM documents WHERE id = ?", (fid,)
            ).fetchone()
            return row["parser_version"]
        time.sleep(0.1)
    raise AssertionError("never READY")


def test_settings_report_docling_readiness(client):
    res = client.get("/api/settings/processing").json()
    assert res["docling_enabled"] is True  # preference defaults to on
    assert res["docling_available"] in (True, False)  # honest readiness report
    if not res["docling_available"]:
        assert res["docling_version"] is None
        assert res["effective_parser"] == "builtin-1"


def test_docling_used_only_when_enabled_and_available(client, settings, monkeypatch):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    fid = _upload_docx(client, pid)
    version = _wait_ready(client, pid, fid)
    assert version == "builtin-1"  # docling not installed → built-in used

    # simulate an installation of docling that successfully parses
    monkeypatch.setattr(parser_mod, "docling_available", lambda: True)
    monkeypatch.setattr(parser_mod, "_try_docling", lambda path: [
        {"type": "heading", "level": 1, "text": "Docling Parsed"},
        {"type": "paragraph", "text": "layout-aware content"},
    ])

    # enabling (already default-on) marks the builtin-parsed doc stale (§11)
    res = client.put("/api/settings/processing", json={"docling_enabled": True}).json()
    assert res["effective_parser"] == "docling-1"
    assert res["marked_stale"] == 1
    doc = next(f for f in client.get(f"/api/projects/{pid}/files").json()["files"]
               if f["id"] == fid)
    assert doc["status"] == "STALE"

    # reprocess → parsed by docling now
    client.post(f"/api/projects/{pid}/files/{fid}/reprocess")
    deadline = time.time() + 10
    while time.time() < deadline:
        status = next(f for f in client.get(f"/api/projects/{pid}/files").json()["files"]
                      if f["id"] == fid)["status"]
        if status == "READY":
            break
        time.sleep(0.1)
    assert _wait_ready(client, pid, fid) == "docling-1"

    # switching off goes stale again and reverts to the built-in parser
    res = client.put("/api/settings/processing", json={"docling_enabled": False}).json()
    assert res["effective_parser"] == "builtin-1"
    assert res["marked_stale"] == 1
    client.post(f"/api/projects/{pid}/files/{fid}/reprocess")
    assert _wait_ready(client, pid, fid) == "builtin-1"

    search = client.post(f"/api/projects/{pid}/search",
                         json={"query": "gateway auth content"}).json()
    assert search["results"]  # reprocessed content searchable


def test_toggle_without_package_installed_is_harmless(client):
    if parser_mod.docling_available():
        return  # real docling installed in this env; nothing to prove here
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    fid = _upload_docx(client, pid)
    _wait_ready(client, pid, fid)
    res = client.put("/api/settings/processing", json={"docling_enabled": True}).json()
    assert res["effective_parser"] == "builtin-1"  # unavailable → no switch
    assert res["marked_stale"] == 0  # nothing changes for existing docs
