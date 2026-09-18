"""OpenAI-compatible provider — the required V1 LLM integration (spec §32).

First-class LiteLLM Gateway compatibility: the model name is passed through
as a gateway alias, the gateway owns provider routing, and the client never
assumes the upstream vendor. Supports chat completions, streaming, tool
calls, timeouts, retries, and categorized error handling.
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterator

import httpx

from .base import ChatMessage, LLMError, LLMResponse, ToolCall, ToolDef


def _categorize(status: int) -> str:
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    if status in (400, 404, 422):
        return "bad_request"
    if status >= 500:
        return "server"
    return "unknown"


class OpenAICompatibleProvider:
    """ Talks to any OpenAI-compatible /chat/completions endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: int = 120,
        max_output_tokens: int | None = None,
        custom_headers: dict | None = None,
        retry_count: int = 2,
        tls_verify: bool = True,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model  # gateway alias — never rewritten (spec §41)
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.default_max_output_tokens = max_output_tokens
        self.custom_headers = custom_headers or {}
        self.retry_count = retry_count
        self._sleep = sleep
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        headers.update(self.custom_headers)
        self.http = httpx.Client(
            timeout=timeout_seconds, headers=headers, verify=tls_verify,
            transport=transport,
        )

    # ------------------------------------------------------------- plumbing
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request_with_retries(self, method: str, url: str, **kw) -> httpx.Response:
        attempts = max(0, self.retry_count) + 1
        last_error: LLMError | None = None
        for attempt in range(attempts):
            try:
                res = self.http.request(method, url, **kw)
            except httpx.TimeoutException as e:
                last_error = LLMError("timeout", f"LLM request timed out: {e}")
            except httpx.HTTPError as e:
                last_error = LLMError("connection", f"Could not reach LLM endpoint: {e}")
            else:
                if res.status_code < 400:
                    return res
                category = _categorize(res.status_code)
                last_error = LLMError(
                    category,
                    self._error_message(res),
                    status=res.status_code,
                )
                # only retry transient failures
                if category not in ("rate_limit", "server"):
                    raise last_error
            if attempt < attempts - 1:
                self._sleep(min(2**attempt, 5))
        raise last_error or LLMError("unknown", "LLM request failed")

    @staticmethod
    def _error_message(res: httpx.Response) -> str:
        try:
            body = res.json()
            detail = body.get("error", {}).get("message") or body.get("detail") or body
        except Exception:
            detail = res.text[:300]
        return f"LLM endpoint returned {res.status_code}: {detail}"

    def _payload(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDef] | None,
        stream: bool,
        max_output_tokens: int | None,
    ) -> dict:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_openai() for m in messages],
            "stream": stream,
        }
        limit = max_output_tokens or self.default_max_output_tokens
        if limit:
            payload["max_tokens"] = limit
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]
            payload["tool_choice"] = "auto"
        return payload

    @staticmethod
    def _parse_response(data: dict) -> LLMResponse:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = [
            ToolCall(
                id=tc.get("id", ""),
                name=tc.get("function", {}).get("name", ""),
                arguments=tc.get("function", {}).get("arguments", "{}"),
            )
            for tc in message.get("tool_calls") or []
        ]
        usage = data.get("usage") or {}
        return LLMResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
            model=data.get("model"),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

    # ------------------------------------------------------------------ API
    def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDef] | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        res = self._request_with_retries(
            "POST",
            self._url("/chat/completions"),
            json=self._payload(messages, tools, False, max_output_tokens),
        )
        return self._parse_response(res.json())

    def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDef] | None = None,
        max_output_tokens: int | None = None,
    ) -> Iterator[dict]:
        """Yield {'delta': str} for content and finally a
        {'response': LLMResponse} item (with any accumulated tool calls)."""
        res = self._request_with_retries(
            "POST",
            self._url("/chat/completions"),
            json=self._payload(messages, tools, True, max_output_tokens),
        )

        # Some gateways ignore `stream` and return a normal JSON completion.
        if (res.headers.get("content-type") or "").startswith("application/json"):
            parsed = self._parse_response(res.json())
            if parsed.content:
                yield {"delta": parsed.content}
            yield {"response": parsed}
            return

        tool_fragments: dict[int, dict] = {}
        finish_reason = None
        for line in res.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            data_str = line[len("data: "):]
            if data_str.strip() == "[DONE]":
                break
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            choice = (event.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            finish_reason = choice.get("finish_reason") or finish_reason
            if delta.get("content"):
                yield {"delta": delta["content"]}
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_fragments.setdefault(
                    idx, {"id": "", "name": "", "arguments": ""}
                )
                slot["id"] = tc.get("id") or slot["id"]
                fn = tc.get("function") or {}
                slot["name"] = slot["name"] or fn.get("name", "")
                slot["arguments"] += fn.get("arguments") or ""

        tool_calls = [
            ToolCall(id=s["id"] or f"call_{i}", name=s["name"], arguments=s["arguments"] or "{}")
            for i, s in sorted(tool_fragments.items())
        ]
        yield {
            "response": LLMResponse(
                content=None,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                model=self.model,
            )
        }

    def health(self) -> dict:
        """Capability/reachability probe used by Test Connection (spec §36)."""
        report: dict[str, Any] = {}
        started = time.monotonic()
        try:
            res = self._request_with_retries("GET", self._url("/models"))
            report["reachable"] = True
            report["authentication"] = "valid"
        except LLMError as e:
            report["reachable"] = e.category not in ("connection", "timeout")
            if e.category == "auth":
                report["authentication"] = "invalid"
            elif e.category in ("connection", "timeout"):
                report["reachable"] = False
                report["error_category"] = e.category
                report["error"] = str(e)
                return report
            else:
                # /models may not exist on minimal gateways; try a chat ping
                report["authentication"] = "valid"
            if e.category not in ("bad_request",):
                report["error_category"] = e.category

        report["latency_ms"] = int((time.monotonic() - started) * 1000)
        try:
            self.complete(
                [ChatMessage(role="user", content="Reply with the single word: ok")],
                max_output_tokens=8,
            )
            report["model_callable"] = True
        except LLMError as e:
            report["model_callable"] = False
            report["error_category"] = e.category
            report["error"] = str(e)
        return report

    def close(self) -> None:
        self.http.close()
