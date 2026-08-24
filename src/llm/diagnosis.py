"""explain_diagnosis: human-readable audit-trail text, written AFTER the
decision. Purely descriptive — never consulted by any decision path, never
fed back into decide() or anything else. Inputs are structured facts only
(category/amount/action/timing/estimated rate/cost), no PII.
"""

import json
import logging
from typing import Optional

from .client import LLMClientInterface
from .cost_tracking import CostTracker
from .types import UsageRecord

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You write a short, factual audit-trail note explaining a payment recovery \
decision that has ALREADY been made by an automated policy. You are not \
making the decision — describe what happened and why, in 1-2 plain \
sentences, for a compliance reviewer. Do not suggest alternatives.
Respond with ONLY the note text."""


def _fixed_format_diagnosis(context: dict) -> str:
    category = context.get("decline_reason", "UNKNOWN")
    action_type = context.get("action_type", "stop")
    offset_minutes = context.get("offset_minutes")
    rate = context.get("estimated_success_rate")
    cost_paise = context.get("cost_paise", 0)

    offset_part = f" at +{offset_minutes:.0f}min" if offset_minutes is not None else ""
    rate_part = f", estimated {rate:.0%} success" if rate is not None else ""
    return (
        f"{category}: {action_type} scheduled{offset_part}{rate_part}, "
        f"cost Rs{cost_paise / 100:.2f}."
    )


def explain_diagnosis(
    context: dict, client: Optional[LLMClientInterface], cost_tracker: Optional[CostTracker] = None
) -> str:
    """context: {"decline_reason": str, "action_type": str, "offset_minutes": float,
    "estimated_success_rate": float, "cost_paise": int, "outage_detected": bool} —
    no PII, no payment_id/customer_id required.

    client=None is a deliberate ablation (full_agent_minus_llm), not a
    failure: goes straight to the fixed format, no network attempt."""
    if client is None:
        return _fixed_format_diagnosis(context)

    try:
        user_content = json.dumps(context, sort_keys=True)
        text, usage = client.explain_diagnosis_raw(SYSTEM_PROMPT, user_content)
    except Exception as exc:
        logger.warning("explain_diagnosis: LLM call failed (%s), falling back to fixed format", exc)
        if cost_tracker is not None:
            cost_tracker.record(
                UsageRecord("explain_diagnosis", used_llm=False, fallback_reason=str(exc))
            )
        return _fixed_format_diagnosis(context)

    if cost_tracker is not None:
        cost_tracker.record(usage)
    if not text:
        logger.warning("explain_diagnosis: LLM response empty, falling back to fixed format")
        return _fixed_format_diagnosis(context)
    return text
