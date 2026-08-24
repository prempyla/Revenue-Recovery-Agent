"""Requirement 4: no PII field reaches the client payload. Asserts on the
ACTUAL content sent to "the model" (FakeLLMClient.calls), not on intent --
draft_message/explain_diagnosis are only ever given structured IDs/facts,
never a name, phone number, or email."""

from llm import CostTracker, FakeLLMClient, draft_message, explain_diagnosis

FORBIDDEN_STRINGS = [
    "pk@gmail.com",  # a real-looking email, as if someone hydrated too early
    "+918121306908",  # a real-looking phone number
    "Manual Verification Test",  # a real-looking name
    "9000000000",  # the synthetic placeholder contact used elsewhere in this repo
]


def _assert_no_forbidden_strings(payload: str):
    for forbidden in FORBIDDEN_STRINGS:
        assert forbidden not in payload, f"PII-shaped string leaked into LLM payload: {forbidden!r}"


def test_draft_message_context_never_contains_pii_shaped_fields():
    client = FakeLLMClient()
    context = {
        "decline_reason": "INSUFFICIENT_FUNDS",
        "action_type": "send_payment_link",
        "amount_paise": 250_000,
        "preferred_language": "en",
        "merchant_name": "Acme Store",
    }
    draft_message(context, client, CostTracker())

    assert len(client.calls) == 1
    sent = client.calls[0]
    assert "system" in sent and "user_content" in sent
    _assert_no_forbidden_strings(sent["system"])
    _assert_no_forbidden_strings(sent["user_content"])
    for pii_key in ("customer_id", "customer_name", "customer_contact", "email", "phone", "contact"):
        assert pii_key not in sent["user_content"]


def test_explain_diagnosis_context_never_contains_pii_shaped_fields():
    client = FakeLLMClient()
    context = {
        "decline_reason": "ISSUER_DOWN",
        "action_type": "retry_scheduled",
        "offset_minutes": 130.0,
        "estimated_success_rate": 0.5,
        "cost_paise": 0,
    }
    explain_diagnosis(context, client, CostTracker())

    sent = client.calls[0]
    _assert_no_forbidden_strings(sent["user_content"])
    for pii_key in ("customer_id", "customer_name", "customer_contact", "email", "phone"):
        assert pii_key not in sent["user_content"]


def test_response_placeholder_tokens_are_never_pre_hydrated_before_the_model_call():
    """The whole point of the placeholder convention: nothing calling
    draft_message ever substitutes a real name into the OUTGOING context --
    hydration happens strictly after, and strictly by the caller."""
    client = FakeLLMClient()
    context = {
        "decline_reason": "AFA_3DS_DROPOFF",
        "action_type": "send_nudge",
        "amount_paise": 100_000,
        "preferred_language": "hinglish",
        "merchant_name": "Acme Store",
    }
    result = draft_message(context, client, None)
    assert "{{customer_name}}" in result
    _assert_no_forbidden_strings(client.calls[-1]["user_content"])


def test_harness_integration_context_built_for_llm_calls_has_no_customer_id():
    """Structural check on harness_integration.run_llm_layer's context dicts
    specifically -- customer_id is available there (from the payment/
    customer objects) but must never be placed into the context passed to
    the model."""
    from datetime import datetime

    from llm.harness_integration import run_llm_layer
    from simulator import ContactTracker, DeclineReason, InstrumentType, Language
    from simulator.harness import LogEntry, PaymentOutcome, RunResult
    from simulator.types import ActionType, Channel, Customer, Payment

    customer = Customer(
        customer_id="cust_pii_check",
        recovery_propensity=0.6,
        funds_arrival_day=2,
        contact_hours=(9, 21),
        annoyance_threshold=10,
        channel_response_rate={"upi_link": 0.7, "whatsapp": 0.6, "sms": 0.5},
        preferred_language=Language.EN,
    )
    payment = Payment(
        payment_id="pay_pii_check",
        customer_id=customer.customer_id,
        amount_paise=250_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=DeclineReason.INSUFFICIENT_FUNDS,
        issuer_code="HDFC",
        failed_at=datetime(2026, 1, 1),
    )
    entry = LogEntry(
        log_id="log_1",
        payment_id=payment.payment_id,
        action_type=ActionType.SEND_PAYMENT_LINK,
        channel=Channel.UPI_LINK.value,
        action_time=datetime(2026, 1, 1, 11, 0),
        is_customer_facing=True,
        policy_name="full_agent",
        reason="test",
        outcome="fail",
        cost_paise=200,
    )
    run_result = RunResult(
        policy_name="full_agent",
        implemented=True,
        log_entries=[entry],
        outcomes=[PaymentOutcome(payment=payment, recovered=False, recovered_at=None)],
    )

    client = FakeLLMClient()
    from llm import CostTracker

    run_llm_layer(run_result, {customer.customer_id: customer}, client, CostTracker())

    assert len(client.calls) == 2  # one draft_message + one explain_diagnosis call
    for call in client.calls:
        assert customer.customer_id not in call["user_content"]
        assert payment.payment_id not in call["user_content"]  # not even our own internal ID
