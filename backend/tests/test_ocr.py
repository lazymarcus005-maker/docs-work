"""Ticket #15: OCR gating, configurable engine, actionable failures (§34, §43)."""
from __future__ import annotations

import io
import time

from app.documents import ocr as ocr_mod


def _upload_image(client, pid):
    # 1x1 transparent PNG
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d4944415478da63fcffff3f030005fe02fea72d1e480000000049454e44ae426082")
    res = client.post(
        f"/api/projects/{pid}/files",
        files={"files": ("scan.png", io.BytesIO(png), "image/png")},
    )
    return res.json()["files"][0]["file_id"]


def _wait_failed(client, pid, fid, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        files = client.get(f"/api/projects/{pid}/files").json()["files"]
        doc = next(f for f in files if f["id"] == fid)
        if doc["status"] == "FAILED":
            return doc
        time.sleep(0.1)
    raise AssertionError(f"never FAILED: {doc}")


def test_ocr_disabled_by_default_image_fails_actionably(client):
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    fid = _upload_image(client, pid)
    doc = _wait_failed(client, pid, fid)
    assert "OCR is disabled" in doc["error"]
    assert "[Enable OCR]" in doc["error"]


def test_ocr_settings_toggle_and_availability_report(client):
    pid = None  # settings are global
    res = client.get("/api/settings/processing").json()
    assert res["ocr_enabled"] is False

    put = client.put("/api/settings/processing",
                     json={"ocr_enabled": True, "ocr_engine": "tesseract"})
    assert put.json()["ocr_enabled"] is True
    assert put.json()["engine_available"] in (True, False)  # honest report

    assert client.put("/api/settings/processing",
                      json={"ocr_enabled": False}).json()["ocr_enabled"] is False


def test_ocr_enabled_without_engine_fails_with_engine_guidance(client):
    if ocr_mod.get_ocr_provider("tesseract").available():
        return  # engine present: the failure path can't be exercised here
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    client.put("/api/settings/processing",
               json={"ocr_enabled": True, "ocr_engine": "tesseract"})
    fid = _upload_image(client, pid)
    doc = _wait_failed(client, pid, fid)
    assert "tesseract" in doc["error"]
    assert "Install" in doc["error"]


def test_ocr_output_preserves_page_references(tmp_path):
    p = ocr_mod.ocr_output_path(tmp_path, "doc_1", 4)
    assert p.name == "doc_1_p4.ocr.txt"
