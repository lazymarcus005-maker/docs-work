"""Ticket #16: project export/import round trip (spec §59, NFR-003)."""
from __future__ import annotations

import io
import zipfile

from app.artifacts import manager


def test_export_import_round_trip(client, settings):
    pid = client.post("/api/projects", json={
        "name": "Exported", "instruction": "Use evidence only.",
    }).json()["id"]
    conn = client.app.state.conn
    client.post(
        f"/api/projects/{pid}/files",
        files={"files": ("doc.txt", io.BytesIO(b"source content"), "text/plain")},
    )
    manager.write_artifact(conn, settings, pid, "req.md", "# Requirements", created_by="skill")

    exported = client.get(f"/api/projects/{pid}/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(exported.content))
    names = archive.namelist()
    assert "metadata.json" in names
    assert "context/doc.txt" in names
    assert "outputs/req.md" in names
    assert not any("secret" in n.lower() for n in names)  # no secrets in packages (§39)

    # import as a new project
    res = client.post("/api/projects/import",
                      files={"file": ("package.zip", io.BytesIO(exported.content), "application/zip")})
    assert res.status_code == 201
    imported = res.json()
    assert imported["id"] != pid
    assert imported["name"] == "Exported"
    assert imported["instruction"] == "Use evidence only."

    files = client.get(f"/api/projects/{imported['id']}/files").json()["files"]
    assert [f["name"] for f in files] == ["doc.txt"]
    outputs = client.get(f"/api/projects/{imported['id']}/artifacts").json()["artifacts"]
    assert [a["file_name"] for a in outputs] == ["req.md"]
    content = client.get(
        f"/api/projects/{imported['id']}/artifacts/{outputs[0]['id']}/content").json()
    assert content["content"] == "# Requirements"


def test_import_rejects_invalid_packages(client):
    res = client.post("/api/projects/import",
                      files={"file": ("x.zip", io.BytesIO(b"not a zip"), "application/zip")})
    assert res.status_code == 422

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("something.txt", "no metadata")
    res = client.post("/api/projects/import",
                      files={"file": ("x.zip", io.BytesIO(buf.getvalue()), "application/zip")})
    assert res.status_code == 422
