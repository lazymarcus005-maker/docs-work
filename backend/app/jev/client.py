"""HTTP client for TypeSafe's structured System One API."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL_ID = "jev-1.13.0"
MAX_STATE_CHARS = 24000
INTENT_OPTIONS = (
    "business_analysis",
    "summarize_sources",
    "general_question",
    "needs_clarification",
)

INTENT_QUESTION = {
    "intent": {
        "type": "choice",
        "instructions": (
            "Classify only the user's requested task. Do not perform the task. "
            "Choose business_analysis for requirements, user stories, or "
            "structured business analysis; summarize_sources for summarizing "
            "or briefing project sources; general_question for ordinary "
            "conversation or questions; needs_clarification when the requested "
            "task cannot be identified."
        ),
        "criteria": {
            "business_analysis": "Create or revise structured BA outputs.",
            "summarize_sources": "Summarize project sources or make a briefing.",
            "general_question": "Answer a general question or continue conversation.",
            "needs_clarification": "The requested task is too ambiguous to classify.",
        },
    }
}


class JevError(Exception):
    """A safe-to-report Jev failure that never includes request credentials."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        latency_ms: int | None = None,
    ) -> None:
        self.status_code = status_code
        self.latency_ms = latency_ms
        super().__init__(message)


@dataclass(frozen=True)
class JevDecision:
    category: str
    confidence: float
    probabilities: dict[str, float]
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0


class JevDecisionClient:
    """Call Jev's typed-question endpoint; it is not a chat-model client."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 20,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("A TypeSafe API key is required")
        self._api_key = api_key
        self._sleep = sleep
        self._client = httpx.Client(timeout=timeout_seconds, transport=transport)

    def classify(self, state: str) -> JevDecision:
        if not isinstance(state, str) or not state.strip():
            raise JevError("The Jev request has no text to classify")
        if len(state) > MAX_STATE_CHARS:
            raise JevError("The request is too long for Jev classification")

        payload = {"state": state, "model": MODEL_ID, "questions": INTENT_QUESTION}
        started = time.monotonic()
        response = None
        for attempt in range(3):
            try:
                response = self._client.post(
                    API_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
            except httpx.TimeoutException:
                raise JevError("TypeSafe request timed out") from None
            except httpx.RequestError:
                raise JevError("TypeSafe could not be reached") from None

            if response.status_code not in (429, 529) or attempt == 2:
                break
            self._sleep(self._retry_delay(response, attempt))

        latency_ms = int((time.monotonic() - started) * 1000)
        if response is None:
            raise JevError("TypeSafe did not return a response", latency_ms=latency_ms)
        if response.status_code >= 400:
            messages = {
                401: "TypeSafe rejected the API key",
                403: "TypeSafe denied this API request",
                422: "TypeSafe rejected the Jev request format",
                429: "TypeSafe rate limit reached; retry later",
                529: "TypeSafe is temporarily overloaded; retry later",
            }
            raise JevError(
                messages.get(response.status_code, "TypeSafe request failed"),
                status_code=response.status_code,
                latency_ms=latency_ms,
            )

        try:
            body = response.json()
        except ValueError:
            raise JevError("TypeSafe returned an invalid response", latency_ms=latency_ms) from None
        return self._parse_decision(body, latency_ms)

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after", "")
        try:
            return min(2.0, max(0.0, float(retry_after)))
        except ValueError:
            return min(2.0, 0.25 * (2 ** attempt))

    @staticmethod
    def _parse_decision(body: object, latency_ms: int) -> JevDecision:
        if not isinstance(body, dict):
            raise JevError("TypeSafe returned an invalid response", latency_ms=latency_ms)
        model = body.get("model")
        answers = body.get("answers")
        answer = answers.get("intent") if isinstance(answers, dict) else None
        if not isinstance(model, str) or not isinstance(answer, dict):
            raise JevError("TypeSafe returned an incomplete response", latency_ms=latency_ms)
        if model != MODEL_ID:
            raise JevError("TypeSafe returned an unexpected model version", latency_ms=latency_ms)
        if answer.get("type") != "choice":
            raise JevError("TypeSafe returned an unexpected answer type", latency_ms=latency_ms)

        category = answer.get("choice")
        confidence = answer.get("confidence")
        probabilities = answer.get("probabilities")
        if not isinstance(category, str) or category not in INTENT_OPTIONS:
            raise JevError("TypeSafe returned an unsupported category", latency_ms=latency_ms)
        if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence) or not 0 <= confidence <= 1):
            raise JevError("TypeSafe returned invalid confidence", latency_ms=latency_ms)
        if not isinstance(probabilities, dict) or set(probabilities) != set(INTENT_OPTIONS):
            raise JevError("TypeSafe returned invalid probabilities", latency_ms=latency_ms)
        normalized = {}
        for name, probability in probabilities.items():
            if (isinstance(probability, bool) or not isinstance(probability, (int, float))
                    or not math.isfinite(probability) or not 0 <= probability <= 1):
                raise JevError("TypeSafe returned invalid probabilities", latency_ms=latency_ms)
            normalized[name] = float(probability)
        if not 0.98 <= sum(normalized.values()) <= 1.02:
            raise JevError("TypeSafe returned invalid probabilities", latency_ms=latency_ms)

        raw_usage = body.get("usage") or {}
        usage = {}
        if isinstance(raw_usage, dict):
            for key in ("input_tokens", "output_tokens"):
                value = raw_usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    usage[key] = value
        return JevDecision(
            category=category,
            confidence=float(confidence),
            probabilities=normalized,
            model=model,
            usage=usage,
            latency_ms=latency_ms,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "JevDecisionClient":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
