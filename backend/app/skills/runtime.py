"""Skill runtime (ticket #8, spec §21.3, §22).

Skills execute under the parent Agent Harness run: they use the same LLM
client, project tools, and context manager, return observations to the
parent, and can never bypass project isolation or tool permissions.
"""
from __future__ import annotations

import json
from typing import Generator

from ..agent import tools as tools_mod
from ..agent.context import build_context_pack, render_context_block
from ..agent.harness import native_decision, prompt_json_decision
from ..llm.base import ChatMessage
from .loader import Skill


def skill_system_prompt(req, skill: Skill, instruction: str) -> str:
    rules = "\n".join(f"- {r}" for r in skill.rules) or "- (none)"
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(skill.workflow, 1)) or "(free-form)"
    project = req.conn.execute(
        "SELECT instruction FROM projects WHERE id = ?", (req.project_id,)
    ).fetchone()
    return (
        f"You are executing the '{skill.name}' skill (v{skill.version}) inside the "
        f"project agent run.\n\n"
        f"Skill description: {skill.description}\n\n"
        f"Skill rules:\n{rules}\n\n"
        f"Skill workflow:\n{steps}\n\n"
        f"Project instruction: {(project['instruction'] if project else '') or '(none)'}\n\n"
        f"Allowed tools: {', '.join(skill.tools) or '(none)'}.\n"
        "Write your output as a file with the write_artifact tool before finishing. "
        "Cite chunk ids for important claims.\n"
    )


def execute_skill_gen(
    req,  # HarnessRequest
    tool_ctx,
    skill: Skill,
    instruction: str,
    run_id: str,
) -> Generator[tuple[str, dict], None, dict]:
    """Run the skill; yields (event, data) pairs, returns the observation dict
    that goes back to the parent harness loop."""
    conn, pid = req.conn, req.project_id
    yield ("skill.started", {"run_id": run_id, "skill": skill.id, "version": skill.version})

    # strict manifest permissions: a skill may only call tools it declares
    allowed = set(skill.tools)
    tool_ctx.skill_id = skill.id
    tool_ctx.skill_version = skill.version
    pack = build_context_pack(
        conn, req.settings, pid, instruction,
        selected_files=req.selected_files,
    )
    messages = [
        ChatMessage(role="system", content=skill_system_prompt(req, skill, instruction)),
        ChatMessage(role="user", content=f"{instruction}\n\n{render_context_block(pack)}"),
    ]

    artifacts: list[str] = []
    summary = ""
    iterations = 0
    tool_defs = tools_mod.tool_defnames(sorted(allowed))
    mode = getattr(req.client, "tool_calling_mode", "auto")
    mode = mode if mode in ("native", "prompt-json") else "native"

    while iterations < 8:
        if req.cancel_event.is_set():
            return {"error": "cancelled"}
        iterations += 1
        if mode == "prompt-json":
            response, deltas = prompt_json_decision(req, messages, tool_defs)
        else:
            response, deltas = native_decision(req, messages, tool_defs)

        if not response.has_tool_calls:
            summary = response.content or "".join(deltas)
            break

        for tc in response.tool_calls:
            if tc.name not in allowed:
                observation = {"error": f"tool {tc.name} is not declared by skill {skill.id}"}
            else:
                try:
                    args = json.loads(tc.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                observation = tools_mod.execute(tool_ctx, tc.name, args)
                if tc.name == "write_artifact" and "error" not in observation:
                    artifacts.append(observation.get("file_name", ""))
            messages.append(ChatMessage(role="assistant", content=None, tool_calls=[tc]))
            messages.append(ChatMessage(
                role="tool", tool_call_id=tc.id, name=tc.name,
                content=json.dumps(observation, ensure_ascii=False),
            ))

    result = {
        "skill": skill.id,
        "summary": summary or "Skill completed.",
        "artifacts": [a for a in artifacts if a],
        "iterations": iterations,
    }
    yield ("skill.completed", {"run_id": run_id, "skill": skill.id, **result})
    return result
