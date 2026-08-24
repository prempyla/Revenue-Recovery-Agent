"""draft_message: happy path, full fallback on error/malformed response, and
the deliberate client=None ablation path."""

from llm import CostTracker, FakeLLMClient, draft_message
from llm.drafting import _template_draft

CONTEXT = {
    "decline_reason": "INSUFFICIENT_FUNDS",
    "action_type": "send_payment_link",
    "amount_paise": 250_000,
    "preferred_language": "hinglish",
    "merchant_name": "Acme Store",
}


def test_happy_path_returns_llm_text_and_records_cost():
    client = FakeLLMClient()
    client.draft_response = "Hi {{customer_name}}, pay via {{payment_link}}"
    cost = CostTracker()

    result = draft_message(CONTEXT, client, cost)

    assert result == client.draft_response
    assert cost.llm_calls() == 1
    assert cost.fallback_calls() == 0


def test_falls_back_to_template_on_exception_and_logs_fallback():
    client = FakeLLMClient()
    client.raise_on_call = TimeoutError("boom")
    cost = CostTracker()

    result = draft_message(CONTEXT, client, cost)

    assert "{{customer_name}}" in result
    assert result == _template_draft(CONTEXT["decline_reason"], CONTEXT["preferred_language"])
    assert cost.llm_calls() == 0
    assert cost.fallback_calls() == 1


def test_falls_back_when_response_is_missing_the_required_placeholder():
    client = FakeLLMClient()
    client.draft_response = "Please pay now, thanks!"  # no {{customer_name}} token
    result = draft_message(CONTEXT, client, None)
    assert result == _template_draft(CONTEXT["decline_reason"], CONTEXT["preferred_language"])


def test_falls_back_when_response_is_empty():
    client = FakeLLMClient()
    client.draft_response = ""
    result = draft_message(CONTEXT, client, None)
    assert "{{customer_name}}" in result


def test_client_none_is_a_deliberate_ablation_not_a_recorded_fallback():
    cost = CostTracker()
    result = draft_message(CONTEXT, None, cost)
    assert "{{customer_name}}" in result
    assert cost.total_calls() == 0  # no attempt was made at all, not a caught failure


def test_every_template_uses_the_same_placeholder_convention_as_the_llm_path():
    for language in ("en", "hi", "hinglish"):
        for decline_reason in ("INSUFFICIENT_FUNDS", "CARD_EXPIRED", "SOMETHING_UNMAPPED"):
            text = _template_draft(decline_reason, language)
            assert "{{customer_name}}" in text
            assert "{{payment_link}}" in text


def test_message_never_contains_real_pii_placeholders_are_not_hydrated_here():
    """draft_message's job stops at the template/LLM text -- hydration with
    a real name/phone/email happens strictly in the caller, never here."""
    client = FakeLLMClient()
    client.draft_response = "Hi {{customer_name}}, pay via {{payment_link}}"
    result = draft_message(CONTEXT, client, None)
    assert "{{customer_name}}" in result  # still a placeholder, not a real name
