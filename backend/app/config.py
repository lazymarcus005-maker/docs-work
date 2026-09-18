"""Application settings.

All runtime configuration comes from environment variables with sensible
local-first defaults. Everything lives under one data directory so the app
is trivially relocatable and backupable.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

APP_VERSION = "0.1.0"


class Settings:
    def __init__(self, env: dict | None = None) -> None:
        env = env if env is not None else os.environ
        base = Path(env.get("COWORK_DATA_DIR", "./data")).resolve()
        self.data_dir = base
        self.workspace_root = Path(
            env.get("COWORK_WORKSPACE_ROOT", str(base / "workspace"))
        ).resolve()
        self.db_path = Path(env.get("COWORK_DB_PATH", str(base / "app.db"))).resolve()
        self.secrets_path = base / "secrets.json"
        self.secret_key_path = base / ".secret_key"

        # Uploads (spec §39)
        self.max_upload_mb = int(env.get("COWORK_MAX_UPLOAD_MB", "200"))

        # CPU worker limits (spec §38)
        self.max_parse_workers = int(env.get("COWORK_MAX_PARSE_WORKERS", "2"))
        self.max_embedding_workers = int(env.get("COWORK_MAX_EMBEDDING_WORKERS", "2"))
        self.max_llm_jobs = int(env.get("COWORK_MAX_LLM_JOBS", "1"))
        self.max_ocr_workers = int(env.get("COWORK_MAX_OCR_WORKERS", "1"))

        # Embedding / retrieval
        self.embedding_model = env.get("COWORK_EMBEDDING_MODEL", "builtin-hash-256")

        # Harness defaults (spec §19)
        self.harness_max_iterations = int(env.get("COWORK_HARNESS_MAX_ITERATIONS", "12"))
        self.harness_max_tool_calls = int(env.get("COWORK_HARNESS_MAX_TOOL_CALLS", "24"))
        self.harness_run_timeout_seconds = int(
            env.get("COWORK_HARNESS_RUN_TIMEOUT", "300")
        )

        # Context manager default budget (§18). Specified in tokens (~4 chars
        # per token heuristic) so the limit is comparable across models.
        self.context_token_budget = int(env.get("COWORK_CONTEXT_TOKEN_BUDGET", "12000"))

        # Harness context compaction (standard agent-harness behavior):
        # window defaults apply when the LLM profile has no override;
        # compaction starts once usage exceeds threshold × working budget.
        self.default_context_window_tokens = int(
            env.get("COWORK_DEFAULT_CONTEXT_WINDOW", "128000"))
        self.context_compact_threshold = float(
            env.get("COWORK_CONTEXT_COMPACT_THRESHOLD", "0.7"))
        self.observation_char_cap = int(
            env.get("COWORK_OBSERVATION_CHAR_CAP", "6000"))
        self.compact_keep_recent = int(
            env.get("COWORK_COMPACT_KEEP_RECENT", "6"))

        # Observability (spec §42). Debug mode may include payload excerpts.
        self.log_level = env.get("COWORK_LOG_LEVEL", "INFO")
        self.debug_payloads = env.get("COWORK_DEBUG_PAYLOADS", "0") == "1"

        # Built-in skills shipped with the application (spec §21)
        self.builtin_skills_dir = Path(__file__).resolve().parent / "builtin_skills"

        self.ensure_dirs()

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
