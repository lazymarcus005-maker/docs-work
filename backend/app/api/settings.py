"""Settings API (ticket #4): LLM profiles CRUD + Test Connection (spec §36,
§41, FR-016). API keys are write-only; reads return masked metadata."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from ..deps import get_db, get_secrets
from ..llm import profiles
from ..secrets import SecretStore

router = APIRouter(prefix="/api/settings", tags=["settings"])

TOOL_MODES = ("auto", "native", "prompt-json")


class ProfileIn(BaseModel):
    name: str = "Untitled profile"
    provider_type: str = "openai-compatible"
    base_url: str
    model: str
    api_key: Optional[str] = None  # write-only, never returned
    timeout_seconds: int = 120
    max_output_tokens: Optional[int] = None
    context_window_override: Optional[int] = None
    tool_calling_mode: str = "auto"
    streaming_enabled: bool = True
    custom_headers: Optional[dict] = None
    retry_count: int = 2
    tls_verify: bool = True
    is_default: bool = False


def _get(conn: sqlite3.Connection, profile_id: str) -> dict:
    p = profiles.get_profile(conn, profile_id)
    if p is None:
        raise HTTPException(status_code=404, detail="LLM profile not found")
    return p


@router.get("/llm-profiles")
def list_llm_profiles(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return {"profiles": [profiles.public_profile(p) for p in profiles.list_profiles(conn)]}


@router.post("/llm-profiles", status_code=201)
def create_llm_profile(
    body: ProfileIn,
    conn: sqlite3.Connection = Depends(get_db),
    secrets: SecretStore = Depends(get_secrets),
) -> dict:
    if body.tool_calling_mode not in TOOL_MODES:
        raise HTTPException(status_code=422, detail="tool_calling_mode must be auto|native|prompt-json")
    p = profiles.save_profile(conn, secrets, body.model_dump())
    return profiles.public_profile(p)


@router.put("/llm-profiles/{profile_id}")
def update_llm_profile(
    profile_id: str,
    body: ProfileIn,
    conn: sqlite3.Connection = Depends(get_db),
    secrets: SecretStore = Depends(get_secrets),
) -> dict:
    _get(conn, profile_id)
    p = profiles.save_profile(conn, secrets, body.model_dump(), profile_id=profile_id)
    return profiles.public_profile(p)


@router.delete("/llm-profiles/{profile_id}", status_code=204)
def delete_llm_profile(
    profile_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    secrets: SecretStore = Depends(get_secrets),
) -> None:
    _get(conn, profile_id)
    profiles.delete_profile(conn, secrets, profile_id)


@router.get("/processing")
def get_processing_settings(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    from ..documents import ocr as ocr_mod

    row = conn.execute("SELECT value FROM settings WHERE key='ocr_engine'").fetchone()
    return {
        "ocr_enabled": ocr_mod.ocr_enabled(conn),
        "ocr_engine": row["value"] if row else "tesseract",
        "engine_available": ocr_mod.get_ocr_provider(
            row["value"] if row else "tesseract").available(),
    }


class ProcessingSettingsIn(BaseModel):
    ocr_enabled: bool
    ocr_engine: str = "tesseract"


@router.put("/processing")
def put_processing_settings(
    body: ProcessingSettingsIn,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    from ..documents import ocr as ocr_mod

    ocr_mod.set_ocr_enabled(conn, body.ocr_enabled, body.ocr_engine)
    return get_processing_settings(conn)


@router.get("/llm-profiles/{profile_id}")
def get_llm_profile(
    profile_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    return profiles.public_profile(_get(conn, profile_id))


@router.post("/llm-profiles/{profile_id}/default")
def set_default_profile(
    profile_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    _get(conn, profile_id)
    conn.execute("UPDATE llm_profiles SET is_default = 0")
    conn.execute("UPDATE llm_profiles SET is_default = 1 WHERE id = ?", (profile_id,))
    conn.commit()
    return profiles.public_profile(_get(conn, profile_id))


@router.post("/llm-profiles/{profile_id}/test")
def test_llm_profile(
    profile_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    secrets: SecretStore = Depends(get_secrets),
    request: Request = None,
) -> dict:
    p = _get(conn, profile_id)
    transport = getattr(request.app.state, "llm_transport", None)
    client = profiles.build_client(conn, secrets, p, transport=transport)
    try:
        report = client.health()
    finally:
        client.close()
    return report
