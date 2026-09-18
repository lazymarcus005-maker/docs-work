"""Agent spend limits (loop-engineering: Token Burn / kill switch).

Token usage is accumulated per run (agent_runs.prompt_tokens /
completion_tokens) and per calendar day (settings table). A daily budget
and a manual kill switch give the owner a hard pause for agent runs.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ..util import now_iso

KILL_SWITCH_KEY = "agent_kill_switch"
BUDGET_KEY = "daily_token_budget"
USAGE_PREFIX = "tokens_used_"


def _today_key() -> str:
    return USAGE_PREFIX + datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def kill_switch_active(conn: sqlite3.Connection) -> bool:
    return _get_setting(conn, KILL_SWITCH_KEY) == "1"


def set_kill_switch(conn: sqlite3.Connection, active: bool) -> None:
    _set_setting(conn, KILL_SWITCH_KEY, "1" if active else "0")


def daily_budget(conn: sqlite3.Connection) -> int:
    raw = _get_setting(conn, BUDGET_KEY)
    try:
        return max(0, int(raw)) if raw else 0
    except ValueError:
        return 0


def set_daily_budget(conn: sqlite3.Connection, tokens: int) -> None:
    _set_setting(conn, BUDGET_KEY, str(max(0, int(tokens))))


def tokens_used_today(conn: sqlite3.Connection) -> int:
    raw = _get_setting(conn, _today_key())
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def record_usage(conn: sqlite3.Connection, prompt: int, completion: int) -> int:
    """Accumulate today's usage; returns the new daily total."""
    key = _today_key()
    total = tokens_used_today(conn) + max(0, prompt) + max(0, completion)
    _set_setting(conn, key, str(total))
    return total


def budget_exceeded(conn: sqlite3.Connection) -> bool:
    budget = daily_budget(conn)
    return budget > 0 and tokens_used_today(conn) >= budget


def limits_status(conn: sqlite3.Connection) -> dict:
    return {
        "kill_switch": kill_switch_active(conn),
        "daily_token_budget": daily_budget(conn),
        "tokens_used_today": tokens_used_today(conn),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "checked_at": now_iso(),
    }
