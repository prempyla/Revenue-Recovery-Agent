"""LLM client interface. Model claude-sonnet-4-6, explicitly requested (not
the skill's opus-5 default) — real, current model, $3/$15 per 1M
input/output tokens verified against Anthropic's own Sonnet 4.6
announcement on 2026-08-25 (see DECISIONS.md), matching the skill's cached
table.

RealAnthropicClient wraps the official SDK, reading ANTHROPIC_API_KEY from
.env. FakeLLMClient is what every test in this repo uses — no network calls
happen from this session. Every raw_* method returns (payload, UsageRecord);
the payload is either a string (drafting/diagnosis) or whatever
classify_reply_raw's tool call returned (validated by
classification._parse_classification_response, never trusted here).
"""

import os
from abc import ABC, abstractmethod
from typing import Any, List, Optional, Tuple

from .types import UsageRecord

MODEL_ID = "claude-sonnet-4-6"

CLASSIFY_REPLY_TOOL = {
    "name": "classify_reply",
    "description": (
        "Classify a customer's SMS/WhatsApp reply to a payment recovery "
        "message into exactly one intent category."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["PROMISE_TO_PAY", "DISPUTE", "WRONG_PERSON", "OPT_OUT", "UNCLEAR"],
            },
            "promised_date": {
                "type": ["string", "null"],
                "description": "ISO 8601 date (YYYY-MM-DD) if intent is PROMISE_TO_PAY and a specific date was mentioned, otherwise null.",
            },
        },
        "required": ["intent", "promised_date"],
        "additionalProperties": False,
    },
    "strict": True,
}


class LLMClientInterface(ABC):
    @abstractmethod
    def draft_message_raw(
        self, system: str, user_content: str, max_tokens: int = 300
    ) -> Tuple[str, UsageRecord]: ...

    @abstractmethod
    def classify_reply_raw(self, system: str, user_content: str) -> Tuple[Any, UsageRecord]:
        """Returns whatever the model's tool call `input` was (or None if it
        didn't call the tool) — UNVALIDATED. classification.py's parsing
        boundary is what turns this into a safe ReplyIntent, never this
        method."""
        ...

    @abstractmethod
    def explain_diagnosis_raw(
        self, system: str, user_content: str, max_tokens: int = 200
    ) -> Tuple[str, UsageRecord]: ...


class RealAnthropicClient(LLMClientInterface):
    def __init__(self, api_key: Optional[str] = None):
        import anthropic

        key = api_key or os.environ["ANTHROPIC_API_KEY"]
        self._client = anthropic.Anthropic(api_key=key)

    def draft_message_raw(
        self, system: str, user_content: str, max_tokens: int = 300
    ) -> Tuple[str, UsageRecord]:
        response = self._client.messages.create(
            model=MODEL_ID,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        return text, UsageRecord(
            function="draft_message",
            used_llm=True,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def classify_reply_raw(self, system: str, user_content: str) -> Tuple[Any, UsageRecord]:
        response = self._client.messages.create(
            model=MODEL_ID,
            max_tokens=256,
            system=system,
            tools=[CLASSIFY_REPLY_TOOL],
            tool_choice={"type": "tool", "name": "classify_reply"},
            messages=[{"role": "user", "content": user_content}],
        )
        tool_use = next((b for b in response.content if b.type == "tool_use"), None)
        raw_input = tool_use.input if tool_use is not None else None
        return raw_input, UsageRecord(
            function="classify_reply",
            used_llm=True,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def explain_diagnosis_raw(
        self, system: str, user_content: str, max_tokens: int = 200
    ) -> Tuple[str, UsageRecord]:
        response = self._client.messages.create(
            model=MODEL_ID,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        return text, UsageRecord(
            function="explain_diagnosis",
            used_llm=True,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


class FakeLLMClient(LLMClientInterface):
    """Test double. Configure `.draft_response` / `.classify_response` /
    `.diagnosis_response` for the happy path, or set `.raise_on_call` to an
    exception instance to simulate every call failing (the load-bearing
    full-harness fallback test uses this). `.calls` records exactly what
    was sent to "the model" — used by the no-PII test to assert on the
    actual payload, not on intent."""

    def __init__(self) -> None:
        self.draft_response: str = "Hi {{customer_name}}, please complete your payment: {{payment_link}}"
        self.classify_response: Any = {"intent": "UNCLEAR", "promised_date": None}
        self.diagnosis_response: str = "Policy scheduled a recovery action for this payment."
        self.raise_on_call: Optional[BaseException] = None
        self.calls: List[dict] = []

    def _maybe_raise(self) -> None:
        if self.raise_on_call is not None:
            raise self.raise_on_call

    def draft_message_raw(
        self, system: str, user_content: str, max_tokens: int = 300
    ) -> Tuple[str, UsageRecord]:
        self.calls.append({"function": "draft_message", "system": system, "user_content": user_content})
        self._maybe_raise()
        return self.draft_response, UsageRecord("draft_message", True, 120, 40)

    def classify_reply_raw(self, system: str, user_content: str) -> Tuple[Any, UsageRecord]:
        self.calls.append({"function": "classify_reply", "system": system, "user_content": user_content})
        self._maybe_raise()
        return self.classify_response, UsageRecord("classify_reply", True, 90, 15)

    def explain_diagnosis_raw(
        self, system: str, user_content: str, max_tokens: int = 200
    ) -> Tuple[str, UsageRecord]:
        self.calls.append({"function": "explain_diagnosis", "system": system, "user_content": user_content})
        self._maybe_raise()
        return self.diagnosis_response, UsageRecord("explain_diagnosis", True, 70, 35)
