"""Harness selection (spec §19.2): the harness implementation is
replaceable. NativeHarness is the required default; ClaudeAgentSDKHarness
is optional and only available when the SDK is installed."""
from __future__ import annotations

from ..harness import NativeHarness

HARNESS_TYPES = ("native", "claude-agent-sdk")


def get_harness(harness_type: str):
    if harness_type in (None, "", "native"):
        return NativeHarness()
    if harness_type == "claude-agent-sdk":
        from .claude_sdk import ClaudeAgentSDKHarness

        return ClaudeAgentSDKHarness()
    raise ValueError(f"unknown harness type: {harness_type}")
