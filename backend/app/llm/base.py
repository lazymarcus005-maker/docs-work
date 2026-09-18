"""LLM client interface and error taxonomy (spec §32).

The provider layer is replaceable (NFR-004); `OpenAICompatibleProvider` is
the required V1 integration (LiteLLM Gateway first-class).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON string, parsed by the caller


@dataclass
class ChatMessage:
    role: str
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # for role=tool messages
    name: str | None = None

    def to_openai(self) -> dict:
        msg: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            msg["content"] = self.content
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id is not None:
            msg["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            msg["name"] = self.name
        return msg


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict  # JSON schema

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall]
    finish_reason: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMError(Exception):
    """An actionable, categorized LLM failure (spec §32.2)."""

    def __init__(self, category: str, message: str, status: int | None = None):
        self.category = category  # auth|rate_limit|timeout|connection|bad_request|server|unknown
        self.status = status
        super().__init__(message)


class LLMClient(Protocol):
    def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDef] | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse: ...

    def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDef] | None = None,
        max_output_tokens: int | None = None,
    ) -> Iterator[dict]: ...

    def health(self) -> dict: ...
