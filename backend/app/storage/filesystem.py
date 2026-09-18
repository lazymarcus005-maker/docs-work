"""Project filesystem layout and safe path handling (spec §9, §39).

Each project maps to a physical local workspace directory:

    <workspace-root>/<project-id>/
        project.yaml  context/  parsed/  knowledge/
        outputs/      sessions/ tmp/     logs/
"""
from __future__ import annotations

import re
from pathlib import Path

SUBDIRS = ("context", "parsed", "knowledge", "outputs", "sessions", "tmp", "logs")

_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]")


def sanitize_filename(name: str) -> str:
    """Strip path components and unsafe characters from a user-supplied name."""
    name = name.replace("\\", "/").split("/")[-1]
    name = _UNSAFE.sub("_", name).strip().strip(".")
    return name or "file"


def project_dir(workspace_root: Path, project_id: str) -> Path:
    """Resolve a project directory, refusing ids that could escape the root."""
    root = Path(workspace_root).resolve()
    candidate = (root / project_id).resolve()
    if candidate.parent != root or not re.fullmatch(r"[A-Za-z0-9_\-]+", project_id):
        raise ValueError(f"invalid project id: {project_id!r}")
    return candidate


def ensure_project_dirs(workspace_root: Path, project_id: str) -> Path:
    pdir = project_dir(workspace_root, project_id)
    for sub in SUBDIRS:
        (pdir / sub).mkdir(parents=True, exist_ok=True)
    return pdir


def safe_join(base: Path, *parts: str) -> Path:
    """Join and verify the result stays inside base (no directory traversal)."""
    resolved = (Path(base) / Path(*parts)).resolve()
    if not str(resolved).startswith(str(Path(base).resolve()) + "/"):
        raise ValueError("path escapes project directory")
    return resolved


def context_dir(workspace_root: Path, project_id: str) -> Path:
    return project_dir(workspace_root, project_id) / "context"


def outputs_dir(workspace_root: Path, project_id: str) -> Path:
    return project_dir(workspace_root, project_id) / "outputs"


def parsed_dir(workspace_root: Path, project_id: str) -> Path:
    return project_dir(workspace_root, project_id) / "parsed"
