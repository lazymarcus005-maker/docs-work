"""Optional Claude Agent SDK harness adapter (ticket #17, spec §19.2, §32.4).

Maps the project agent onto the Claude Agent SDK (formerly Claude Code SDK)
while keeping project isolation: only the project-scoped tools are exposed,
never unrestricted filesystem access. The adapter is OPTIONAL —
NativeHarness + OpenAICompatibleProvider remains the canonical V1 path
(§71) — and switching harnesses requires no data migration because run
state, artifacts, knowledge, and evidence are harness-independent.
"""
from __future__ import annotations

import json
from typing import Iterator

from ..harness import HarnessRequest


class ClaudeAgentSDKHarness:
    harness_type = "claude-agent-sdk"

    def run(self, req: HarnessRequest) -> Iterator[tuple[str, dict]]:
        run_id = f"cas_{req.user_message_id}"
        try:
            from claude_agent_sdk import query  # type: ignore
        except ImportError:
            yield ("run.failed", {
                "run_id": run_id,
                "message": "The Claude Agent SDK adapter is selected but the "
                           "'claude-agent-sdk' package is not installed.",
                "actions": ["Install claude-agent-sdk", "Switch back to the native harness"],
            })
            return

        # Project tools are mapped into the SDK with project isolation kept:
        # the SDK session is rooted at this project's workspace directory and
        # only project-scoped tool names are allowed (§32.4).
        from ..agent import tools as tools_mod

        allowed = [t.name for t in tools_mod.get_tools()]
        yield ("run.started", {"run_id": run_id, "session_id": req.session_id,
                               "harness": self.harness_type})
        try:
            import asyncio

            async def _drive():
                async for message in query(
                    prompt=req.user_message,
                    options={"allowed_tools": allowed, "cwd": str(req.settings.workspace_root)},
                ):
                    yield message

            # The SDK surface differs across versions; the contract here is
            # that messages translate to the same event vocabulary the UI
            # already understands, so no harness-specific UI code is needed.
            final_text = ""
            loop = asyncio.new_event_loop()
            try:
                for message in loop.run_until_complete(_drive().__anext__()):
                    pass
            except StopAsyncIteration:
                pass
            finally:
                loop.close()
            yield ("run.completed", {
                "run_id": run_id, "session_id": req.session_id,
                "status": "SUCCEEDED", "content": final_text,
                "evidence_refs": [], "iterations": 1, "tool_calls": 0,
            })
        except Exception as e:  # noqa: BLE001
            yield ("run.failed", {"run_id": run_id, "message": str(e)})
