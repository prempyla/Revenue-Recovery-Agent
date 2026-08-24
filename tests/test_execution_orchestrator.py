"""Isolation tests for diagnose_and_schedule: the policy -> state -> outbox
wiring, and the policy_stop ABANDONED reason path."""

from datetime import datetime

from simulator.types import Customer, DeclineReason, InstrumentType, Language, Payment

from execution.db import make_engine, make_session_factory
from execution.eventlog import derive_state, history
from execution.orchestrator import diagnose_and_schedule
from execution.states import AbandonReason, PaymentState


def _session():
    engine = make_engine("sqlite:///:memory:")
    return make_session_factory(engine)()


PERSONA = Customer(
    customer_id="cust_x",
    recovery_propensity=0.6,
    funds_arrival_day=2,
    contact_hours=(9, 21),
    annoyance_threshold=5,
    channel_response_rate={"upi_link": 0.7, "whatsapp": 0.6, "sms": 0.5},
    preferred_language=Language.EN,
)


def _payment(decline_reason: DeclineReason, instrument_type=InstrumentType.CARD) -> Payment:
    return Payment(
        payment_id="pay_x",
        customer_id=PERSONA.customer_id,
        amount_paise=250_000,
        instrument_type=instrument_type,
        decline_reason=decline_reason,
        issuer_code="HDFC",
        failed_at=datetime(2026, 1, 1),
    )


def test_insufficient_funds_schedules_a_send_payment_link_intent():
    session = _session()
    payment = _payment(DeclineReason.INSUFFICIENT_FUNDS)
    now = datetime(2026, 1, 1)

    intent = diagnose_and_schedule(session, payment, PERSONA, now)

    assert intent is not None
    assert intent.action_type == "send_payment_link"
    assert intent.payload["amount_paise"] == payment.amount_paise
    assert derive_state(session, "pay_x") == PaymentState.SCHEDULED
    assert [e.to_state for e in history(session, "pay_x")] == [
        "at_risk",
        "diagnosed",
        "scheduled",
    ]


def test_mandate_revoked_abandons_with_policy_stop_reason():
    session = _session()
    payment = _payment(DeclineReason.MANDATE_REVOKED, InstrumentType.MANDATE)
    now = datetime(2026, 1, 1)

    intent = diagnose_and_schedule(session, payment, PERSONA, now)

    assert intent is None
    assert derive_state(session, "pay_x") == PaymentState.ABANDONED
    last_event = history(session, "pay_x")[-1]
    assert last_event.abandon_reason == AbandonReason.POLICY_STOP.value


def test_non_payment_link_action_abandons_with_policy_stop_reason():
    """NETWORK_TIMEOUT routes rules_only to retry_now, which isn't this
    round's execution path -- orchestrator abandons rather than scheduling
    something the worker can't execute."""
    session = _session()
    payment = _payment(DeclineReason.NETWORK_TIMEOUT)
    now = datetime(2026, 1, 1)

    intent = diagnose_and_schedule(session, payment, PERSONA, now)

    assert intent is None
    assert derive_state(session, "pay_x") == PaymentState.ABANDONED
    last_event = history(session, "pay_x")[-1]
    assert last_event.abandon_reason == AbandonReason.POLICY_STOP.value
    assert "retry_now" in last_event.payload["reason"]
