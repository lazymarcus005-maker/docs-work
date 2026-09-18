"""NativeHarness — the provider-neutral agent loop (ticket #7, spec §19).

Every user message enters the harness. The default execution model is an
iterative loop, not a single prompt-response call:

    while not finished:
        decision = llm(context)            # stream for the final answer
        if tool_call: execute, observe, continue
        if ask_user: pause with WAITING_USER
        else: final answer

Stop controls: max_iterations, max_tool_calls, run_timeout, user_cancel,
explicit_finish. Run state is persisted and events stream to the UI.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from typing import Any, Iterator

from ..config import Settings
from ..llm.base import ChatMessage, LLMClient, LLMResponse, ToolCall
from ..util import log_event
from . import run_state, spend, tools as tools_mod
from .compaction import (compact_messages, context_usage, should_compact,
                         truncate_text, working_budget)
from .context import build_context_pack, document_ids_for_files, render_context_block
from .tools import ToolContext

CHUNK_ID_RE = re.compile(r"\bchk_[A-Za-z0-9_]+")

SYSTEM_RULES = (
    "You are the project agent of a local document workspace.\n"
    "Rules:\n"
    "- Operate only within the provided project context; never invent missing project facts.\n"
    "- Prefer evidence-backed answers; cite chunk ids (e.g. [chk_x]) for important claims.\n"
    "- Identify uncertainty and conflicting sources explicitly.\n"
    "- Prefer tools over guessing when project information is needed.\n"
    "- If and only if the user's request cannot proceed without their input, call ask_user.\n"
    "- When a task needs a reusable skill, call run_skill with the best-matching skill id.\n"
)


class StopRun(Exception):
    def __init__(self, reason: str, data: str | None = None):
        self.reason = reason
        self.data = data
        super().__init__(reason)


class HarnessRequest:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        client: LLMClient,
        project_id: str,
        session_id: str,
        user_message: str,
        user_message_id: str,
        selected_files: list[str] | None = None,
        skill_id: str | None = None,
        cancel_event: threading.Event | None = None,
        max_iterations: int = 12,
        max_tool_calls: int = 24,
        timeout_seconds: int = 300,
        max_context_tokens: int | None = None,
        context_window_tokens: int = 0,
        max_output_tokens: int | None = None,
        compact_threshold: float = 0.7,
        keep_recent: int = 6,
        observation_char_cap: int = 6000,
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.client = client
        self.project_id = project_id
        self.session_id = session_id
        self.user_message = user_message
        self.user_message_id = user_message_id
        self.selected_files = selected_files or []
        self.skill_id = skill_id
        self.cancel_event = cancel_event or threading.Event()
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls
        self.timeout_seconds = timeout_seconds
        self.max_context_tokens = max_context_tokens  # §19.1 absolute backstop
        self.context_window_tokens = context_window_tokens  # from the profile
        self.max_output_tokens = max_output_tokens
        self.compact_threshold = compact_threshold
        self.keep_recent = keep_recent
        self.observation_char_cap = observation_char_cap
        self.observed_prompt_tokens: int | None = None
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0


def _sse_pair(event: str, data: dict):
    return (event, data)


class NativeHarness:
    """Provider-neutral default harness — REQUIRED path (spec §19.2)."""

    harness_type = "native"

    def run(self, req: HarnessRequest) -> Iterator[tuple[str, dict]]:
        conn, project_id = req.conn, req.project_id
        run_id = run_state.create_run(
            conn, project_id, req.session_id, req.user_message_id,
            self.harness_type, llm_profile_id=None,
            selected_skill=req.skill_id,
        )
        yield _sse_pair("run.started", {"run_id": run_id, "session_id": req.session_id})
        log_event(conn, "agent.run.started", project_id,
                  {"run_id": run_id, "harness": self.harness_type})
        conn.commit()

        started = time.monotonic()
        tool_ctx = ToolContext(
            conn, req.settings, project_id,
            selected_document_ids=document_ids_for_files(conn, project_id, req.selected_files),
            session_id=req.session_id,
        )
        tool_names = [t.name for t in tools_mod.get_tools()] + ["ask_user", "run_skill"]

        tool_defs = tools_mod.tool_defnames(
            [n for n in tool_names if n not in ("ask_user", "run_skill")]
        )
        from ..llm.base import ToolDef

        tool_defs.append(ToolDef(
            "ask_user",
            "Pause the run and ask the user a clarifying question.",
            {"type": "object",
             "properties": {"question": {"type": "string"}},
             "required": ["question"], "additionalProperties": False},
        ))
        tool_defs.append(ToolDef(
            "run_skill",
            "Run a named skill, e.g. {\"skill\": \"ba\", \"instruction\": \"...\"}.",
            {"type": "object",
             "properties": {"skill": {"type": "string"}, "instruction": {"type": "string"}},
             "required": ["skill"], "additionalProperties": False},
        ))
        pack, context_block, events_ahead = None, "", []
        yield _sse_pair("context.search.started", {"run_id": run_id})
        pack = build_context_pack(
            conn, req.settings, project_id, req.user_message,
            selected_files=req.selected_files,
        )
        context_block = render_context_block(pack)
        yield _sse_pair("context.search.completed", {
            "run_id": run_id,
            "sources": [s["document"] for s in pack["sources"]],
            "truncated": pack["truncated"],
        })

        project = conn.execute(
            "SELECT instruction FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        project_instruction = project["instruction"] if project else ""

        # durable AI memory (spec §26): context, never instructions (§27)
        from ..knowledge.memory import render_memory_block

        system_content = (
            f"{SYSTEM_RULES}\n"
            f"Workspace instruction:\n{_global_instruction(conn)}\n\n"
            f"Project instruction:\n{project_instruction}\n\n"
            "Project memory (durable facts from earlier sessions — respect"
            f" these, do not contradict them):\n{render_memory_block(conn, project_id)}\n\n"
            f"{context_block}\n\n"
            f"Available tools: {', '.join(tool_names)}\n"
            "run_skill: run a named skill with {\"skill\": id, \"instruction\": text}.\n"
            "ask_user: pause the run and ask the user a clarifying question."
        )

        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=system_content),
        ]
        for m in _session_history(conn, req.session_id):
            messages.append(m)
        messages.append(ChatMessage(role="user", content=req.user_message))
        mode = _tool_mode(req)

        # explicit skill invocation runs before the free loop (spec §54)
        if req.skill_id:
            yield from _invoke_skill(req, tool_ctx, run_id, req.skill_id,
                                     req.user_message, messages)
        final_text = ""
        status = "SUCCEEDED"
        error_code = None
        iterations = 0
        tool_calls_total = 0

        try:
            while True:
                if req.cancel_event.is_set():
                    raise StopRun("cancelled")
                if spend.kill_switch_active(conn):
                    raise StopRun("kill_switch")
                if spend.budget_exceeded(conn):
                    raise StopRun("daily_budget")
                if iterations >= req.max_iterations:
                    raise StopRun("max_iterations")
                if tool_calls_total >= req.max_tool_calls:
                    raise StopRun("max_tool_calls")

                # standard harness behavior: compact instead of failing when
                # the conversation grows past the model's usable window (§18)
                budget = working_budget(
                    req.context_window_tokens, req.max_output_tokens)
                usage = context_usage(messages, req.observed_prompt_tokens)
                if should_compact(usage, budget, req.compact_threshold):
                    yield _sse_pair("context.compacting", {
                        "run_id": run_id, "usage_tokens": usage,
                        "budget_tokens": budget,
                    })
                    messages, comp = compact_messages(
                        messages, req.client, keep_recent=req.keep_recent)
                    if comp["compacted"]:
                        yield _sse_pair("context.compacted", {
                            "run_id": run_id,
                            "before_tokens": comp["before"],
                            "after_tokens": comp["after"],
                            "summarized_messages": comp["summarized_messages"],
                            "fallback": comp["fallback"],
                        })
                        log_event(conn, "context.compacted", project_id, {
                            "run_id": run_id,
                            "before": comp["before"], "after": comp["after"],
                        })
                        conn.commit()

                # absolute backstop — compaction should prevent reaching this
                if req.max_context_tokens and context_usage(
                        messages, req.observed_prompt_tokens) > req.max_context_tokens:
                    raise StopRun("max_context_tokens")
                if time.monotonic() - started > req.timeout_seconds:
                    raise StopRun("run_timeout")
                iterations += 1
                run_state.update_run(conn, run_id, iteration_count=iterations)

                if mode == "prompt-json":
                    response, deltas = prompt_json_decision(req, messages, tool_defs)
                else:
                    response, deltas = native_decision(req, messages, tool_defs)
                if response.prompt_tokens or response.completion_tokens:
                    req.total_prompt_tokens += response.prompt_tokens or 0
                    req.total_completion_tokens += response.completion_tokens or 0
                    req.observed_prompt_tokens = max(
                        req.observed_prompt_tokens or 0,
                        response.prompt_tokens or 0)
                    run_state.update_run(
                        conn, run_id,
                        prompt_tokens=req.total_prompt_tokens,
                        completion_tokens=req.total_completion_tokens)

                if response.has_tool_calls:
                    for tc in response.tool_calls:
                        if tool_calls_total >= req.max_tool_calls:
                            raise StopRun("max_tool_calls")
                        tool_calls_total += 1
                        run_state.update_run(conn, run_id, tool_call_count=tool_calls_total)
                        for ev in _execute_tool(req, conn, tool_ctx, run_id, tc, messages):
                            yield ev
                    continue

                # final answer — release the buffered deltas
                final_text = "".join(deltas)
                for delta in _chunk(final_text):
                    if req.cancel_event.is_set():
                        raise StopRun("cancelled")
                    yield _sse_pair("assistant.delta", {"run_id": run_id, "delta": delta})
                break

        except StopRun as e:
            if e.reason == "cancelled":
                status, error_code = "CANCELLED", None
                yield _sse_pair("run.cancelled", {"run_id": run_id})
            elif e.reason == "waiting_user":
                status, error_code = "WAITING_USER", None
                final_text = e.data or ""
                yield _sse_pair("run.waiting_user", {"run_id": run_id})
            elif e.reason == "kill_switch":
                status, error_code = "FAILED", "kill_switch"
                yield _sse_pair("run.failed", {
                    "run_id": run_id,
                    "message": "The agent kill switch is active, so runs are paused.",
                    "actions": ["Turn the kill switch off in Settings → Agent limits"],
                })
            elif e.reason == "daily_budget":
                status, error_code = "FAILED", "daily_budget"
                yield _sse_pair("run.failed", {
                    "run_id": run_id,
                    "message": "The daily token budget for agent runs is exhausted.",
                    "actions": ["Raise or clear the daily budget in Settings → Agent limits",
                                "Wait until tomorrow (usage resets daily)"],
                })
            else:
                status, error_code = "FAILED", e.reason
                yield _sse_pair("run.failed", {
                    "run_id": run_id,
                    "message": f"The agent run stopped: {e.reason}.",
                    "actions": ["Retry the request", "Raise the harness limits in settings"],
                })
        except Exception as e:  # noqa: BLE001 — surfaced as run.failed
            status, error_code = "FAILED", "internal_error"
            yield _sse_pair("run.failed", {"run_id": run_id, "message": str(e)})

        run_state.update_run(
            conn, run_id, status=status, error_code=error_code,
            tool_call_count=tool_calls_total,
            prompt_tokens=req.total_prompt_tokens,
            completion_tokens=req.total_completion_tokens,
            completed_at=_now(),
        )
        try:
            spend.record_usage(conn, req.total_prompt_tokens,
                               req.total_completion_tokens)
        except Exception:  # noqa: BLE001 — accounting must not break runs
            pass
        log_event(conn, f"agent.run.{status.lower()}",
                  project_id, {"run_id": run_id})
        conn.commit()

        evidence_refs = sorted(set(CHUNK_ID_RE.findall(final_text)))
        yield _sse_pair("run.completed", {
            "run_id": run_id,
            "session_id": req.session_id,
            "status": status,
            "content": final_text,
            "evidence_refs": evidence_refs,
            "iterations": iterations,
            "tool_calls": tool_calls_total,
            "prompt_tokens": req.total_prompt_tokens,
            "completion_tokens": req.total_completion_tokens,
        })


def _now() -> str:
    from ..util import now_iso

    return now_iso()


def _global_instruction(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'global_instruction'"
    ).fetchone()
    return (row["value"] if row else "") or ""


def _session_history(conn: sqlite3.Connection, session_id: str, limit: int = 10) -> list[ChatMessage]:
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ?"
        " ORDER BY rowid DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    # older → newer, excluding the current user message is handled by the
    # caller appending it last (history here excludes the newest message)
    rows = list(reversed(rows))
    return [ChatMessage(role=r["role"], content=r["content"]) for r in rows[:-1]]


def _tool_mode(req: HarnessRequest) -> str:
    profile_mode = getattr(req.client, "tool_calling_mode", "auto")
    if profile_mode in ("native", "prompt-json"):
        return profile_mode
    return "native"


# ------------------------------------------------------------- decisions
def native_decision(
    req: HarnessRequest, messages: list[ChatMessage], tool_defs: list
) -> tuple[LLMResponse, list[str]]:
    deltas: list[str] = []
    response: LLMResponse | None = None
    for chunk in req.client.stream(messages, tools=tool_defs or None):
        if "delta" in chunk:
            deltas.append(chunk["delta"])
        elif "response" in chunk:
            response = chunk["response"]
    if response is None:  # provider without streaming assembled response
        response = req.client.complete(messages, tools=tool_defs or None)
    return response, deltas


PROMPT_JSON_INSTRUCTION = (
    "Respond with a single JSON object and nothing else. Either:\n"
    '{"action": {"tool": "<tool name>", "arguments": {...}}}\n'
    "or\n"
    '{"answer": "<final answer to the user>"}\n'
)


def prompt_json_decision(
    req: HarnessRequest, messages: list[ChatMessage], tool_names: list[str]
) -> tuple[LLMResponse, list[str]]:
    import jsonschema

    msgs = list(messages)
    if msgs and msgs[0].role == "system":
        msgs[0] = ChatMessage(role="system", content=msgs[0].content + "\n\n" + PROMPT_JSON_INSTRUCTION)
    response = req.client.complete(msgs)
    raw = (response.content or "").strip()
    try:
        parsed = json.loads(_strip_code_fence(raw))
        jsonschema.validate(
            parsed,
            {
                "type": "object",
                "anyOf": [
                    {"required": ["action"]},
                    {"required": ["answer"]},
                ],
            },
        )
    except Exception:
        # one retry with a correction nudge, then fail loudly
        msgs.append(ChatMessage(role="user", content="Your last reply was not valid action JSON. Reply again with exactly one JSON object."))
        response = req.client.complete(msgs)
        parsed = json.loads(_strip_code_fence((response.content or "").strip()))
    if "answer" in parsed:
        return LLMResponse(content=parsed["answer"], tool_calls=[]), [parsed["answer"]]
    action = parsed["action"]
    tc = ToolCall(
        id=f"pj_{int(time.monotonic() * 1000)}",
        name=action.get("tool", ""),
        arguments=json.dumps(action.get("arguments") or {}),
    )
    return LLMResponse(content=None, tool_calls=[tc]), []


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _chunk(text: str, size: int = 64):
    for i in range(0, len(text), size):
        yield text[i:i + size]


# ------------------------------------------------------------ execution
def _invoke_skill(
    req: HarnessRequest,
    tool_ctx: ToolContext,
    run_id: str,
    skill_id: str,
    instruction: str,
    messages: list[ChatMessage],
) -> Iterator[tuple[str, dict]]:
    """Load and run a skill under this run, appending its observation."""
    from ..skills import loader, runtime

    skill = loader.get_skill(req.conn, skill_id)
    if skill is None:
        observation = {"error": f"skill not found: {skill_id}"}
    else:
        gen = runtime.execute_skill_gen(req, tool_ctx, skill, instruction, run_id)
        observation = {}
        while True:
            try:
                yield next(gen)
            except StopIteration as stop:
                observation = stop.value or {}
                break

    messages.append(ChatMessage(
        role="assistant",
        content=f"[skill {skill_id} result] {json.dumps(observation, ensure_ascii=False)}",
    ))


def _execute_tool(
    req: HarnessRequest,
    conn: sqlite3.Connection,
    tool_ctx: ToolContext,
    run_id: str,
    tc: ToolCall,
    messages: list[ChatMessage],
) -> Iterator[tuple[str, dict]]:
    name = tc.name
    try:
        args = json.loads(tc.arguments or "{}")
    except json.JSONDecodeError:
        args = {}

    yield _sse_pair("tool.started", {"run_id": run_id, "tool": name, "arguments": args})
    log_event(conn, "agent.tool.called", req.project_id, {"tool": name})
    conn.commit()

    if name == "ask_user":
        question = args.get("question") or args.get("message") or "Could you clarify?"
        run_state.update_run(conn, run_id, status="WAITING_USER")
        messages.append(ChatMessage(role="assistant", content=question))
        raise StopRun("waiting_user", data=question)

    if name == "run_skill":
        skill_id = args.get("skill") or ""
        observation = {}
        for ev in _invoke_skill(req, tool_ctx, run_id, skill_id,
                                args.get("instruction") or req.user_message, messages):
            if ev[0] == "skill.completed":
                observation = ev[1]
            yield ev
        run_state.update_run(conn, run_id, skill_call_count=(run_state.get_run(conn, run_id) or {}).get("skill_call_count", 0) + 1)
        yield _sse_pair("tool.completed", {
            "run_id": run_id, "tool": name,
            "summary": f"skill {skill_id}: {observation.get('summary', observation.get('error', 'done'))}",
        })
        messages.append(ChatMessage(role="tool", tool_call_id=tc.id, name=name,
                                    content=json.dumps(observation, ensure_ascii=False)))
        return

    observation = tools_mod.execute(tool_ctx, name, args)

    yield _sse_pair("tool.completed", {
        "run_id": run_id, "tool": name,
        "summary": _observation_summary(name, observation),
    })
    messages.append(ChatMessage(role="assistant", content=None, tool_calls=[tc]))
    messages.append(ChatMessage(
        role="tool", tool_call_id=tc.id, name=name,
        content=truncate_text(json.dumps(observation, ensure_ascii=False),
                              req.observation_char_cap)))


def _observation_summary(name: str, observation: dict) -> str:
    if "error" in observation:
        return observation["error"]
    if "results" in observation:
        return f"{len(observation['results'])} passages found"
    if "chunks" in observation:
        return f"{len(observation['chunks'])} chunks found"
    if "files" in observation:
        return f"{len(observation['files'])} files"
    return "done"
