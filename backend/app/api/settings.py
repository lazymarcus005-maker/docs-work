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
from ..jev import JevDecisionClient, JevError
from ..jev.client import MODEL_ID
from ..jev import settings as jev_settings

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
    from ..documents.parser import docling_available, docling_version
    from ..documents.pipeline import docling_enabled, effective_parser_version

    row = conn.execute("SELECT value FROM settings WHERE key='ocr_engine'").fetchone()
    available = docling_available()
    return {
        "ocr_enabled": ocr_mod.ocr_enabled(conn),
        "ocr_engine": row["value"] if row else "tesseract",
        "engine_available": ocr_mod.get_ocr_provider(
            row["value"] if row else "tesseract").available(),
        "docling_enabled": docling_enabled(conn),
        "docling_available": available,
        "docling_version": docling_version() if available else None,
        "effective_parser": effective_parser_version(conn),
    }


class ProcessingSettingsIn(BaseModel):
    ocr_enabled: Optional[bool] = None
    ocr_engine: str = "tesseract"
    docling_enabled: Optional[bool] = None


@router.put("/processing")
def put_processing_settings(
    body: ProcessingSettingsIn,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    from ..documents import ocr as ocr_mod
    from ..documents.pipeline import mark_stale, set_docling_enabled

    if body.ocr_enabled is not None:
        ocr_mod.set_ocr_enabled(conn, body.ocr_enabled, body.ocr_engine)
    marked_stale = 0
    if body.docling_enabled is not None:
        set_docling_enabled(conn, body.docling_enabled)
        # parser configuration changed: existing indexes may be stale (§11)
        marked_stale = mark_stale(conn)
    result = get_processing_settings(conn)
    result["marked_stale"] = marked_stale
    return result


@router.get("/limits")
def get_limits(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    from ..agent import spend

    return spend.limits_status(conn)


class LimitsIn(BaseModel):
    daily_token_budget: Optional[int] = None
    kill_switch: Optional[bool] = None


@router.put("/limits")
def put_limits(
    body: LimitsIn, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    from ..agent import spend

    if body.daily_token_budget is not None:
        spend.set_daily_budget(conn, body.daily_token_budget)
    if body.kill_switch is not None:
        spend.set_kill_switch(conn, body.kill_switch)
    return spend.limits_status(conn)


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
    client = profiles.build_client(
        conn, secrets, p, transport=transport,
        session_id=f"connection-test-{profile_id}",
    )
    try:
        report = client.health()
    finally:
        client.close()
    return report


class JevSettingsIn(BaseModel):
    enabled: Optional[bool] = None
    api_key: Optional[str] = None
    clear_api_key: bool = False


@router.get("/internal-tools/jev")
def get_jev_settings(
    conn: sqlite3.Connection = Depends(get_db),
    secrets: SecretStore = Depends(get_secrets),
) -> dict:
    return jev_settings.public_settings(conn, secrets)


@router.put("/internal-tools/jev")
def put_jev_settings(
    body: JevSettingsIn,
    conn: sqlite3.Connection = Depends(get_db),
    secrets: SecretStore = Depends(get_secrets),
) -> dict:
    if body.clear_api_key:
        if body.api_key:
            raise HTTPException(
                status_code=422,
                detail="Provide a new api_key or clear_api_key, not both",
            )
        if body.enabled is True:
            raise HTTPException(
                status_code=422,
                detail="Jev cannot be enabled while clearing its API key",
            )
        jev_settings.set_enabled(conn, False)
        try:
            secrets.delete(jev_settings.SECRET_REF)
        except Exception:
            raise HTTPException(
                status_code=500, detail="Could not remove the TypeSafe API key"
            ) from None
        return jev_settings.public_settings(conn, secrets)

    if body.api_key:
        try:
            secrets.set(jev_settings.SECRET_REF, body.api_key)
        except Exception:
            raise HTTPException(
                status_code=500, detail="Could not save the TypeSafe API key"
            ) from None

    if body.enabled is not None:
        if body.enabled and not jev_settings.has_api_key(secrets):
            raise HTTPException(
                status_code=409,
                detail="Add a TypeSafe API key before enabling Jev",
            )
        jev_settings.set_enabled(conn, body.enabled)

    return jev_settings.public_settings(conn, secrets)


@router.post("/internal-tools/jev/test")
def test_jev_connection(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    secrets: SecretStore = Depends(get_secrets),
) -> dict:
    api_key = secrets.get(jev_settings.SECRET_REF)
    if not api_key:
        raise HTTPException(
            status_code=409, detail="Add a TypeSafe API key before testing Jev"
        )
    jev = JevDecisionClient(
        api_key,
        transport=getattr(request.app.state, "jev_transport", None),
    )
    try:
        decision = jev.classify(
            "Synthetic Jev connectivity check. No project or document data is included."
        )
        return {
            "reachable": True,
            "model": decision.model or MODEL_ID,
            "latency_ms": decision.latency_ms,
            "usage": decision.usage,
        }
    except JevError as exc:
        return {
            "reachable": False,
            "model": MODEL_ID,
            "latency_ms": exc.latency_ms,
            "error": str(exc),
        }
    finally:
        jev.close()
