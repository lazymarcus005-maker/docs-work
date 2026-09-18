"""LLM profile storage + client resolution (spec §32.1, §41)."""
from __future__ import annotations

import sqlite3
from typing import Any

from ..secrets import SecretStore
from .openai_compatible import OpenAICompatibleProvider

SECRET_PREFIX = "secret://llm-profiles/"


def profile_secret_ref(profile_id: str) -> str:
    return f"{SECRET_PREFIX}{profile_id}"


def _row_to_dict(row) -> dict:
    return dict(row)


def get_profile(conn: sqlite3.Connection, profile_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM llm_profiles WHERE id = ?", (profile_id,)
    ).fetchone()
    return _row_to_dict(row) if row else None


def default_profile(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM llm_profiles WHERE is_default = 1 ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM llm_profiles ORDER BY created_at LIMIT 1"
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_profiles(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM llm_profiles ORDER BY created_at").fetchall()
    return [_row_to_dict(r) for r in rows]


def save_profile(
    conn: sqlite3.Connection,
    secrets: SecretStore,
    fields: dict[str, Any],
    profile_id: str | None = None,
) -> dict:
    """Create or update a profile. `api_key` inside fields is write-only."""
    from ..util import new_id, now_iso

    ts = now_iso()
    pid = profile_id or new_id("lp")
    existing = get_profile(conn, pid) if profile_id else None

    api_key = fields.pop("api_key", None)
    if api_key:
        secrets.set(profile_secret_ref(pid), api_key)

    cols = {
        "name": fields.get("name", "Untitled profile"),
        "provider_type": fields.get("provider_type", "openai-compatible"),
        "base_url": fields.get("base_url", ""),
        "model": fields.get("model", ""),
        "timeout_seconds": int(fields.get("timeout_seconds", 120)),
        "max_output_tokens": fields.get("max_output_tokens"),
        "context_window_override": fields.get("context_window_override"),
        "tool_calling_mode": fields.get("tool_calling_mode", "auto"),
        "streaming_enabled": 1 if fields.get("streaming_enabled", True) else 0,
        "custom_headers": fields.get("custom_headers") or "{}",
        "retry_count": int(fields.get("retry_count", 2)),
        "tls_verify": 1 if fields.get("tls_verify", True) else 0,
    }
    if isinstance(cols["custom_headers"], dict):
        import json

        cols["custom_headers"] = json.dumps(cols["custom_headers"])

    if existing:
        sets = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(
            f"UPDATE llm_profiles SET {sets}, updated_at = ? WHERE id = ?",
            (*cols.values(), ts, pid),
        )
    else:
        columns = ["id", "api_key_ref", "created_at", "updated_at", *cols]
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO llm_profiles ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            (pid, profile_secret_ref(pid), ts, ts, *cols.values()),
        )

    if fields.get("is_default"):
        conn.execute("UPDATE llm_profiles SET is_default = 0")
        conn.execute("UPDATE llm_profiles SET is_default = 1 WHERE id = ?", (pid,))
    elif not existing and conn.execute(
        "SELECT COUNT(*) AS n FROM llm_profiles WHERE is_default = 1"
    ).fetchone()["n"] == 0:
        conn.execute("UPDATE llm_profiles SET is_default = 1 WHERE id = ?", (pid,))

    conn.commit()
    return get_profile(conn, pid)


def delete_profile(conn: sqlite3.Connection, secrets: SecretStore, profile_id: str) -> None:
    secrets.delete(profile_secret_ref(profile_id))
    conn.execute("DELETE FROM llm_profiles WHERE id = ?", (profile_id,))
    conn.commit()


def build_client(
    conn: sqlite3.Connection,
    secrets: SecretStore,
    profile: dict,
    transport=None,
) -> OpenAICompatibleProvider:
    import json

    key = secrets.get(profile["api_key_ref"] or "") if profile.get("api_key_ref") else None
    client = OpenAICompatibleProvider(
        base_url=profile["base_url"],
        model=profile["model"],
        api_key=key,
        timeout_seconds=profile["timeout_seconds"],
        max_output_tokens=profile["max_output_tokens"],
        custom_headers=json.loads(profile["custom_headers"] or "{}"),
        retry_count=profile["retry_count"],
        tls_verify=bool(profile["tls_verify"]),
        transport=transport,
    )
    client.tool_calling_mode = profile["tool_calling_mode"] or "auto"
    return client


def public_profile(profile: dict) -> dict:
    """Masked metadata only — the API key itself never leaves the store."""
    out = dict(profile)
    out.pop("api_key_ref", None)
    for key in ("is_default", "streaming_enabled", "tls_verify"):
        out[key] = bool(out.get(key))
    out["has_api_key"] = bool(profile.get("api_key_ref"))
    return out
