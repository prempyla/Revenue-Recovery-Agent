"""draft_message: outbound customer copy. No PII ever reaches the model —
inputs are exclusively our own structured, non-PII facts (decline_reason,
action_type, amount_paise, preferred_language). The returned text uses
literal {{customer_name}} / {{payment_link}} placeholder tokens; the caller
hydrates real values locally, after the model has already returned. This is
what makes "no PII to the model" structural rather than a prompt request.
"""

import json
import logging
from typing import Optional

from .client import LLMClientInterface
from .cost_tracking import CostTracker
from .types import UsageRecord

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are drafting a short customer-facing payment recovery message for a \
fintech merchant. You do not have access to any customer's name, phone \
number, or email — use the literal placeholder token {{customer_name}} \
exactly where a name would go, and {{payment_link}} where a link goes. \
Never invent a name, phone number, or email.

Write in the requested language. "hinglish" means natural code-switched \
Hindi/English in Latin script, the way Indian customers commonly text.

Under 300 characters, one message, no signature block.
Respond with ONLY the message text — no preamble, no markdown, no quotes."""

# Fallback templates, keyed by (decline_reason, preferred_language). Every
# entry uses the same {{customer_name}}/{{payment_link}} placeholder
# convention as the LLM path, so hydration is identical either way.
_TEMPLATES = {
    ("INSUFFICIENT_FUNDS", "en"): "Hi {{customer_name}}, your recent payment didn't go through. Pay anytime here: {{payment_link}}",
    ("INSUFFICIENT_FUNDS", "hi"): "Hi {{customer_name}}, aapka payment nahi ho paya. Yahan se kar dijiye: {{payment_link}}",
    ("INSUFFICIENT_FUNDS", "hinglish"): "Hi {{customer_name}}, aapka payment fail ho gaya tha. Jab convenient ho, yahan se complete kar dijiye: {{payment_link}}",
    ("MANDATE_INSUFFICIENT_FUNDS", "en"): "Hi {{customer_name}}, your scheduled payment couldn't go through. Complete it here: {{payment_link}}",
    ("MANDATE_INSUFFICIENT_FUNDS", "hi"): "Hi {{customer_name}}, aapka scheduled payment nahi ho paya. Yahan se karein: {{payment_link}}",
    ("MANDATE_INSUFFICIENT_FUNDS", "hinglish"): "Hi {{customer_name}}, aapka auto-payment fail ho gaya. Yahan se complete kar dein: {{payment_link}}",
    ("CARD_EXPIRED", "en"): "Hi {{customer_name}}, your card on file has expired. Update it here: {{payment_link}}",
    ("CARD_EXPIRED", "hi"): "Hi {{customer_name}}, aapka card expire ho gaya hai. Update karein: {{payment_link}}",
    ("CARD_EXPIRED", "hinglish"): "Hi {{customer_name}}, aapka saved card expire ho gaya hai. Yahan se update kar dijiye: {{payment_link}}",
    ("CARD_OR_ACCOUNT_BLOCKED", "en"): "Hi {{customer_name}}, we couldn't charge your card. Please update your payment method: {{payment_link}}",
    ("CARD_OR_ACCOUNT_BLOCKED", "hi"): "Hi {{customer_name}}, aapka card charge nahi ho paya. Payment method update karein: {{payment_link}}",
    ("CARD_OR_ACCOUNT_BLOCKED", "hinglish"): "Hi {{customer_name}}, card se charge nahi hua. Please payment method update kar dijiye: {{payment_link}}",
    ("AFA_3DS_DROPOFF", "en"): "Hi {{customer_name}}, you were close! Complete your payment here: {{payment_link}}",
    ("AFA_3DS_DROPOFF", "hi"): "Hi {{customer_name}}, aapka payment adhoora reh gaya tha. Yahan se poora karein: {{payment_link}}",
    ("AFA_3DS_DROPOFF", "hinglish"): "Hi {{customer_name}}, aapka payment beech mein reh gaya tha. Yahan se complete kar dijiye: {{payment_link}}",
}
_DEFAULT_TEMPLATE = {
    "en": "Hi {{customer_name}}, please complete your pending payment here: {{payment_link}}",
    "hi": "Hi {{customer_name}}, apna pending payment yahan se complete karein: {{payment_link}}",
    "hinglish": "Hi {{customer_name}}, apna pending payment yahan se complete kar dijiye: {{payment_link}}",
}


def _template_draft(decline_reason: str, preferred_language: str) -> str:
    return _TEMPLATES.get(
        (decline_reason, preferred_language),
        _DEFAULT_TEMPLATE.get(preferred_language, _DEFAULT_TEMPLATE["en"]),
    )


def draft_message(
    context: dict, client: Optional[LLMClientInterface], cost_tracker: Optional[CostTracker] = None
) -> str:
    """context: {"decline_reason": str, "action_type": str, "amount_paise": int,
    "preferred_language": str, "merchant_name": str} — no customer_id, no PII.

    client=None is a deliberate ablation (full_agent_minus_llm), not a
    failure: goes straight to the template path, no network attempt, and
    is not recorded as a fallback-due-to-error."""
    if client is None:
        return _template_draft(context.get("decline_reason", ""), context.get("preferred_language", "en"))

    try:
        user_content = json.dumps(context, sort_keys=True)
        text, usage = client.draft_message_raw(SYSTEM_PROMPT, user_content)
    except Exception as exc:
        logger.warning("draft_message: LLM call failed (%s), falling back to template", exc)
        if cost_tracker is not None:
            cost_tracker.record(UsageRecord("draft_message", used_llm=False, fallback_reason=str(exc)))
        return _template_draft(context.get("decline_reason", ""), context.get("preferred_language", "en"))

    if cost_tracker is not None:
        cost_tracker.record(usage)
    if not text or "{{customer_name}}" not in text:
        # Malformed/degenerate response (e.g. missing the required
        # placeholder) -- same fallback path, not a partial trust.
        logger.warning("draft_message: LLM response malformed, falling back to template")
        return _template_draft(context.get("decline_reason", ""), context.get("preferred_language", "en"))
    return text
