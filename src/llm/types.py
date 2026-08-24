"""Types for the LLM layer.

ReplyIntent is a closed enum by construction — nothing in this codebase can
produce a ReplyIntent value outside the five listed members. See
classification.py's _parse_classification_response for the actual
enforcement (the security boundary); this file just declares the type.
"""

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional


class ReplyIntent(str, Enum):
    PROMISE_TO_PAY = "PROMISE_TO_PAY"
    DISPUTE = "DISPUTE"
    WRONG_PERSON = "WRONG_PERSON"
    OPT_OUT = "OPT_OUT"
    UNCLEAR = "UNCLEAR"


@dataclass(frozen=True)
class ClassifiedReply:
    intent: ReplyIntent
    promised_date: Optional[date] = None
    source: str = "llm"  # "llm" | "fallback_regex" — for cost/audit reporting
    truncated: bool = False  # True if the input text exceeded MAX_REPLY_TEXT_CHARS


@dataclass(frozen=True)
class UsageRecord:
    """One LLM call's token usage, or one fallback event (used_llm=False,
    zero tokens either way). Fed into cost_tracking.CostTracker."""

    function: str  # "draft_message" | "classify_reply" | "explain_diagnosis"
    used_llm: bool
    input_tokens: int = 0
    output_tokens: int = 0
    fallback_reason: Optional[str] = None
