"""Skill runtime (ticket #8, spec §21.3, §22).

Skills execute under the parent Agent Harness run: they use the same LLM
client, project tools, and context manager, return observations to the
parent, and can never bypass project isolation or tool permissions.
"""
from __future__ import annotations

import json
from typing import Generator

from ..agent import tools as tools_mod
from ..agent.compaction import truncate_text
from ..agent.context import build_context_pack, render_context_block
from ..agent.harness import native_decision, prompt_json_decision
from ..llm.base import ChatMessage
from ..util import new_id, now_iso
from .loader import Skill


def skill_system_prompt(req, skill: Skill, instruction: str) -> str:
    rules = "\n".join(f"- {r}" for r in skill.rules) or "- (none)"
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(skill.workflow, 1)) or "(free-form)"
    skill_instructions = skill.prompt.strip() or "(none)"
    output_rule = (
        "Write your output as a file with the write_artifact tool before finishing."
        if "write_artifact" in skill.tools
        else "Return the requested output in the final response; do not claim to have created an artifact."
    )
    project = req.conn.execute(
        "SELECT instruction FROM projects WHERE id = ?", (req.project_id,)
    ).fetchone()
    from ..knowledge.memory import render_memory_block

    return (
        f"You are executing the '{skill.name}' skill (v{skill.version}) inside the "
        f"project agent run.\n\n"
        f"Skill description: {skill.description}\n\n"
        f"Skill instructions:\n{skill_instructions}\n\n"
        f"Skill rules:\n{rules}\n\n"
        f"Skill workflow:\n{steps}\n\n"
        f"Project instruction: {(project['instruction'] if project else '') or '(none)'}\n\n"
        "Project memory (durable facts — respect, do not contradict):\n"
        f"{render_memory_block(req.conn, req.project_id)}\n\n"
        f"Allowed tools: {', '.join(skill.tools) or '(none)'}.\n"
        f"{output_rule} Cite chunk ids for important claims.\n"
    )


def execute_skill_gen(
    req,  # HarnessRequest
    tool_ctx,
    skill: Skill,
    instruction: str,
    run_id: str,
) -> Generator[tuple[str, dict], None, dict]:
    """Run the skill; yields (event, data) pairs, returns the observation dict
    that goes back to the parent harness loop.

    Loop-engineering safeguards:
    - validation fix attempts are capped (3); beyond that the artifact is
      escalated to the Review Queue instead of retrying forever
    - an independent verifier (fresh LLM call, REJECT stance) reviews every
      written artifact when the project's autonomy level allows
    - autonomy L1 turns artifact writes into proposals awaiting approval
    """
    conn, pid = req.conn, req.project_id
    autonomy = _autonomy_level(conn, pid)
    yield ("skill.started", {"run_id": run_id, "skill": skill.id,
                             "version": skill.version, "autonomy": autonomy})

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

    artifacts: list[dict] = []
    pending_approval: list[str] = []
    validation_failures = 0
    escalated = False
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
            yield ("tool.started", {"run_id": run_id, "tool": tc.name})
            if escalated and tc.name in ("write_artifact", "update_artifact", "run_validator"):
                # fix-attempt cap reached: no more blind retries (anti-pattern:
                # infinite fix loop) — the escalation is already queued
                observation = {"error": "validation attempts exhausted — escalated to review queue"}
            elif tc.name not in allowed:
                observation = {"error": f"tool {tc.name} is not declared by skill {skill.id}"}
            else:
                try:
                    args = json.loads(tc.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                observation = tools_mod.execute(tool_ctx, tc.name, args)
                observation = _post_process(
                    req, conn, pid, run_id, tc.name, observation,
                    artifacts, pending_approval, autonomy)
                if tc.name == "run_validator" and "error" not in observation:
                    if observation.get("validation_status") == "failed":
                        validation_failures += 1
                    elif observation.get("validation_status") == "passed":
                        validation_failures = 0
                    if validation_failures >= 3 and not escalated:
                        escalated = True
                        _escalate_validation(
                            conn, pid, run_id, skill.id, args.get("file_name", ""),
                            validation_failures, observation)
                        yield ("skill.escalated", {
                            "run_id": run_id, "skill": skill.id,
                            "reason": "validation_failed_repeatedly",
                            "attempts": validation_failures,
                            "file": args.get("file_name", ""),
                        })
            summary_text = observation.get("summary") or observation.get("file_name")
            if not summary_text:
                summary_text = observation.get("error") or "Completed"
            yield ("tool.completed", {
                "run_id": run_id, "tool": tc.name,
                "summary": str(summary_text)[:240],
                "error": bool(observation.get("error")),
            })
            messages.append(ChatMessage(role="assistant", content=None, tool_calls=[tc]))
            messages.append(ChatMessage(
                role="tool", tool_call_id=tc.id, name=tc.name,
                content=truncate_text(json.dumps(observation, ensure_ascii=False),
                                      req.observation_char_cap),
            ))

    result = {
        "skill": skill.id,
        "summary": summary or "Skill completed.",
        "artifacts": [a["file_name"] for a in artifacts if a.get("file_name")],
        "proposed": [a["file_name"] for a in artifacts if a.get("proposed")],
        "escalated": escalated,
        "iterations": iterations,
    }
    yield ("skill.completed", {"run_id": run_id, "skill": skill.id, **result})
    return result


def _autonomy_level(conn, project_id: str) -> int:  # noqa: ANN001
    row = conn.execute(
        "SELECT autonomy_level FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    return row["autonomy_level"] if row and row["autonomy_level"] else 2


def _post_process(req, conn, pid, run_id, tool_name, observation,
                  artifacts, pending_approval, autonomy) -> dict:
    """Post-process artifact writes: verifier check (L2/L3), proposal flow
    (L1), and bookkeeping for the skill result."""
    if tool_name not in ("write_artifact", "update_artifact") or "error" in observation:
        return observation
    artifact_id = observation.get("artifact_id")
    version = observation.get("version")
    file_name = observation.get("file_name", "")
    if not artifact_id or not version:
        return observation

    entry = {"file_name": file_name, "artifact_id": artifact_id, "version": version}

    if autonomy == 1:
        # propose-only: hold for explicit user approval (spec §19 gate)
        conn.execute(
            "UPDATE artifacts SET validation_status = 'proposed' WHERE id = ?",
            (artifact_id,),
        )
        conn.execute(
            "INSERT INTO review_items (id, project_id, kind, payload, suggestion,"
            " status, created_at) VALUES (?, ?, 'artifact_approval', ?, ?, 'open', ?)",
            (new_id("rev"), pid,
             json.dumps({"artifact_id": artifact_id, "file_name": file_name,
                         "run_id": run_id}),
             json.dumps({"action": "approve"}), now_iso()),
        )
        conn.commit()
        entry["proposed"] = True
        pending_approval.append(file_name)
        artifacts.append(entry)
        return {**observation, "proposed": True,
                "note": "autonomy L1: awaiting user approval"}

    # L2/L3: independent verifier (fresh call, REJECT stance)
    from ..artifacts import manager
    from ..knowledge.memory import load_active_memories  # noqa: F401 (context parity)
    from .verifier import verify_artifact, attach_verifier_result

    try:
        version_row = manager.read_version(conn, pid, artifact_id)
    except Exception:  # noqa: BLE001
        return observation
    result = verify_artifact(
        conn, req.client, pid, file_name,
        version_row["content"], json.loads(version_row["source_context"] or "[]"))
    attach_verifier_result(conn, pid, artifact_id, version, result)
    entry["verifier"] = result["verdict"]
    observation = {**observation, "verifier": result["verdict"],
                   "verifier_issues": result.get("issues", [])}
    artifacts.append(entry)
    return observation


def _escalate_validation(conn, pid, run_id, skill_id, file_name, attempts,
                         last_result) -> None:  # noqa: ANN001
    """Fix-attempt cap reached: queue full context for a human decision."""
    conn.execute(
        "INSERT INTO review_items (id, project_id, kind, payload, suggestion,"
        " status, created_at) VALUES (?, ?, 'validation_escalation', ?, ?,"
        " 'open', ?)",
        (new_id("rev"), pid,
         json.dumps({"skill_id": skill_id, "file_name": file_name,
                     "attempts": attempts, "run_id": run_id,
                     "last_result": last_result}, ensure_ascii=False),
         json.dumps({"action": "review"}), now_iso()),
    )
    conn.commit()
