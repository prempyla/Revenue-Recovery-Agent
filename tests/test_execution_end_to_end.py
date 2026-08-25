"""The actual end-to-end path: policy decides -> action persisted -> worker
executes (fake client, no network) -> simulated webhook confirms -> state
updated. This is the single route this round: INSUFFICIENT_FUNDS ->
rules_only -> send_payment_link."""

from datetime import datetime, timezone

from simulator.types import Customer, DeclineReason, InstrumentType, Language, Payment

from execution.db import make_engine, make_session_factory
from execution.eventlog import derive_state, history
from execution.orchestrator import diagnose_and_schedule
from execution.outbox import run_outbox_worker_once
from execution.razorpay_client import FakeRazorpayClient
from execution.states import PaymentState
from execution.webhook import process_webhook_event

PERSONA = Customer(
    customer_id="cust_e2e",
    recovery_propensity=0.6,
    funds_arrival_day=2,
    contact_hours=(9, 21),
    annoyance_threshold=5,
    channel_response_rate={"upi_link": 0.7, "whatsapp": 0.6, "sms": 0.5},
    preferred_language=Language.EN,
)

PAYMENT = Payment(
    payment_id="pay_e2e",
    customer_id=PERSONA.customer_id,
    amount_paise=500_000,
    instrument_type=InstrumentType.CARD,
    decline_reason=DeclineReason.INSUFFICIENT_FUNDS,
    issuer_code="HDFC",
    failed_at=datetime(2026, 1, 1),
)


def _session():
    engine = make_engine("sqlite:///:memory:")
    return make_session_factory(engine)()


def test_full_path_ends_in_recovered_when_webhook_confirms_payment():
    session = _session()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    intent = diagnose_and_schedule(session, PAYMENT, PERSONA, now)
    assert intent is not None
    assert derive_state(session, "pay_e2e") == PaymentState.SCHEDULED

    client = FakeRazorpayClient()
    # 2026-08-25: rules_only's INSUFFICIENT_FUNDS offset is 1 hour -- the
    # worker must be polled with a `now` past due_at, not the same `now`
    # the intent was scheduled at, or it's correctly skipped as not-yet-due.
    worker_now = intent.due_at
    processed = run_outbox_worker_once(session, client, worker_now, "worker_1")
    assert processed[0].status == "done"
    assert derive_state(session, "pay_e2e") == PaymentState.AWAITING_CONFIRMATION

    real_call = client.calls[0]
    assert real_call["amount_paise"] == PAYMENT.amount_paise
    assert real_call["reference_id"] == intent.idempotency_key

    webhook_payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {"entity": {"reference_id": intent.idempotency_key, "id": processed[0].result["id"]}}
        },
    }
    process_webhook_event(session, webhook_payload, worker_now)

    assert derive_state(session, "pay_e2e") == PaymentState.RECOVERED
    assert [e.to_state for e in history(session, "pay_e2e")] == [
        "at_risk",
        "diagnosed",
        "scheduled",
        "executing",
        "awaiting_confirmation",
        "recovered",
    ]


def test_full_path_ends_in_abandoned_when_webhook_reports_expiry():
    session = _session()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    intent = diagnose_and_schedule(session, PAYMENT, PERSONA, now)
    client = FakeRazorpayClient()
    # See the recovered-path test above: must poll the worker past due_at,
    # not at the scheduling time, or this test degenerates into checking a
    # SCHEDULED->ABANDONED transition that never actually went through the
    # worker at all (still legal per the FSM, but not what this test claims
    # to exercise).
    worker_now = intent.due_at
    processed = run_outbox_worker_once(session, client, worker_now, "worker_1")
    assert processed[0].status == "done"
    assert derive_state(session, "pay_e2e") == PaymentState.AWAITING_CONFIRMATION

    webhook_payload = {
        "event": "payment_link.expired",
        "payload": {"payment_link": {"entity": {"reference_id": intent.idempotency_key}}},
    }
    process_webhook_event(session, webhook_payload, worker_now)

    assert derive_state(session, "pay_e2e") == PaymentState.ABANDONED
    last_event = history(session, "pay_e2e")[-1]
    assert last_event.abandon_reason == "payment_failed"
