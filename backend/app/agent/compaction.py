"""Context compaction (spec §18/§19.1) — standard agent-harness behavior.

Instead of failing a run when the conversation grows past the model's
context window, the harness compacts: older turns (including tool
observations) are summarized into a single context note, while the system
prompt, the most recent turns, and every chunk citation stay intact.

Token counts use the provider's reported prompt usage when available and
fall back to the ~4-chars-per-token estimate.
"""
from __future__ import annotations

from ..llm.base import ChatMessage, LLMClient, LLMResponse

SUMMARY_MARKER = "[Earlier conversation summary — kept for context]"
SUMMARY_SYSTEM = (
    "You are the context compactor of an agent harness. Summarize the earlier "
    "conversation turns so work can continue seamlessly. Preserve: decisions "
    "made, facts established, user preferences, file and entity names, "
    "numbers, open questions, and every chunk id citation (chk_...). Be "
    "concise: at most 300 words. Output only the summary."
)
FALLBACK_SUMMARY_MARKER = "[Earlier conversation — auto-truncated summary]"


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def messages_tokens(messages: list[ChatMessage]) -> int:
    return sum(estimate_tokens(m.content or "") for m in messages)


def context_usage(messages: list[ChatMessage], observed_prompt_tokens: int | None) -> int:
    """Best-known size of the next prompt: provider usage if we have it,
    otherwise the estimate."""
    return max(observed_prompt_tokens or 0, messages_tokens(messages))


def should_compact(usage: int, working_budget: int, threshold: float) -> bool:
    return working_budget > 0 and usage > working_budget * threshold


def truncate_text(text: str, cap: int) -> str:
    """Head+tail truncation for oversized tool observations."""
    if cap <= 0 or len(text) <= cap:
        return text
    head = int(cap * 0.6)
    tail = max(cap - head, 0)
    dropped = len(text) - head - tail
    return f"{text[:head]}\n…[{dropped} chars truncated]…\n{text[len(text) - tail:]}"


def working_budget(context_window_tokens: int, max_output_tokens: int | None,
                   safety_margin: int = 2000) -> int:
    """Window minus the room the model needs to answer, minus jitter."""
    reserve = (max_output_tokens or 4096) + safety_margin
    return max(context_window_tokens - reserve, 0)


def _is_summary_note(m: ChatMessage) -> bool:
    return (m.content or "").startswith((SUMMARY_MARKER, FALLBACK_SUMMARY_MARKER))


def compact_messages(
    messages: list[ChatMessage],
    llm: LLMClient,
    keep_recent: int = 6,
) -> tuple[list[ChatMessage], dict]:
    """Return (compacted_messages, info). Never raises: if the summarizer
    fails, a mechanical truncation summary is used instead.

    Guarantees: the system prompt survives; the first genuine user message
    (the task anchor) is preserved verbatim; prior summary notes are folded
    into the new summary so they never stack up."""
    info = {"compacted": False, "before": messages_tokens(messages),
            "summarized_messages": 0, "fallback": False}
    if len(messages) < 3:
        return messages, info

    system = messages[0] if messages[0].role == "system" else None
    body = messages[1:] if system else messages[:]

    anchor = next((m for m in body if m.role == "user"
                   and not _is_summary_note(m)), None)
    if anchor is not None:
        body = [m for m in body if m is not anchor]
    prior_notes = [m for m in body if _is_summary_note(m)]
    rest = [m for m in body if not _is_summary_note(m)]

    keep = max(2, keep_recent)
    if len(rest) <= keep:
        return messages, info
    old, recent = rest[:-keep], rest[-keep:]
    if not old:
        return messages, info

    prior_text = "\n\n".join(
        (m.content or "").split("\n", 1)[-1].strip() for m in prior_notes)
    transcript = "\n\n".join(
        f"[{m.role}] {(m.content or '(tool call)')[:1500]}" for m in old)
    if prior_text:
        transcript = f"[Previous summary]\n{prior_text}\n\n" + transcript

    summary, fallback = None, False
    try:
        resp: LLMResponse = llm.complete([
            ChatMessage(role="system", content=SUMMARY_SYSTEM),
            ChatMessage(role="user",
                        content="Summarize these earlier turns:\n\n" + transcript),
        ])
        summary = (resp.content or "").strip() or None
    except Exception:  # noqa: BLE001 — compaction must never kill the run
        summary = None
    if not summary:
        fallback = True
        summary = truncate_text(transcript, 1600)

    note = ChatMessage(
        role="user",
        content=f"{FALLBACK_SUMMARY_MARKER if fallback else SUMMARY_MARKER}\n{summary}",
    )
    new_messages = ([system] if system else []) + [note] \
        + ([anchor] if anchor else []) + recent
    info.update(compacted=True, before=messages_tokens(messages),
                after=messages_tokens(new_messages),
                summarized_messages=len(old), fallback=fallback)
    return new_messages, info
