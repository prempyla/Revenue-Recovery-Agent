"""Isolation tests for the reconciliation poller. FakeRazorpayClient only,
no network."""

from datetime import datetime, timedelta

from execution.db import make_engine, make_session_factory
from execution.eventlog import append_event, derive_state, history
from execution.idempotency import make_idempotency_key
from execution.outbox import write_intent_with_state_change
from execution.razorpay_client import FakeRazorpayClient
from execution.reconciliation import run_reconciliation_poll_once
from execution.states import AbandonReason, PaymentState

STALENESS = 30  # minutes, matches config.RECONCILIATION_STALENESS_MINUTES


def _session():
    engine = make_engine("sqlite:///:memory:")
    return make_session_factory(engine)()


def _to_diagnosed(session, payment_id, now):
    append_event(session, payment_id, PaymentState.AT_RISK, now)
    append_event(session, payment_id, PaymentState.DIAGNOSED, now)
    session.commit()


def _schedule_and_create_link(session, client, payment_id, now, attempt=1):
    """Gets a payment all the way to a real link existing at Razorpay, then
    returns (idem_key, remote_result) without necessarily advancing local
    state past SCHEDULED -- callers push it further (or not) to set up the
    specific stuck scenario under test."""
    _to_diagnosed(session, payment_id, now)
    idem_key = make_idempotency_key(payment_id, attempt)
    write_intent_with_state_change(
        session,
        payment_id,
        idem_key,
        "send_payment_link",
        {"amount_paise": 100_000, "customer_name": "C", "customer_contact": "9000000000"},
        now,
    )
    remote = client.create_payment_link(
        amount_paise=100_000, customer_name="C", customer_contact="9000000000", reference_id=idem_key
    )
    return idem_key, remote


def test_stuck_executing_with_created_remote_link_catches_up_to_awaiting_confirmation():
    session = _session()
    client = FakeRazorpayClient()
    now = datetime(2026, 1, 1)
    idem_key, _ = _schedule_and_create_link(session, client, "pay_x", now)
    # Simulate: the API call actually succeeded, but the process crashed
    # right after, before run_outbox_worker_once recorded it -- state is
    # still stuck at EXECUTING even though a real link exists.
    append_event(session, "pay_x", PaymentState.EXECUTING, now)
    session.commit()

    poll_time = now + timedelta(minutes=STALENESS + 1)
    results = run_reconciliation_poll_once(session, client, poll_time, staleness_minutes=STALENESS)

    assert len(results) == 1
    assert results[0].action == "caught_up_to_awaiting_confirmation"
    assert derive_state(session, "pay_x") == PaymentState.AWAITING_CONFIRMATION
    last_event = history(session, "pay_x")[-1]
    assert last_event.payload["source"] == "reconciliation_poll"


def test_stuck_executing_with_paid_remote_status_reconciles_straight_to_recovered():
    session = _session()
    client = FakeRazorpayClient()
    now = datetime(2026, 1, 1)
    idem_key, _ = _schedule_and_create_link(session, client, "pay_x", now)
    append_event(session, "pay_x", PaymentState.EXECUTING, now)
    session.commit()
    client.set_status(idem_key, "paid")  # customer paid; we just never found out

    poll_time = now + timedelta(minutes=STALENESS + 1)
    results = run_reconciliation_poll_once(session, client, poll_time, staleness_minutes=STALENESS)

    assert results[0].action == "reconciled_to_recovered"
    assert derive_state(session, "pay_x") == PaymentState.RECOVERED
    # Both the catch-up and the final transition happened, both tagged.
    events = history(session, "pay_x")
    assert [e.to_state for e in events[-2:]] == ["awaiting_confirmation", "recovered"]
    assert all(e.payload["source"] == "reconciliation_poll" for e in events[-2:])


def test_stuck_executing_with_no_remote_record_abandons_with_execution_error():
    session = _session()
    client = FakeRazorpayClient()
    now = datetime(2026, 1, 1)
    _to_diagnosed(session, "pay_x", now)
    idem_key = make_idempotency_key("pay_x", 1)
    write_intent_with_state_change(
        session, "pay_x", idem_key, "send_payment_link",
        {"amount_paise": 100_000, "customer_name": "C", "customer_contact": "9000000000"}, now,
    )
    append_event(session, "pay_x", PaymentState.EXECUTING, now)
    session.commit()
    # Deliberately never call client.create_payment_link -- simulates the
    # call never actually reaching Razorpay before the crash.

    poll_time = now + timedelta(minutes=STALENESS + 1)
    results = run_reconciliation_poll_once(session, client, poll_time, staleness_minutes=STALENESS)

    assert results[0].action == "abandoned_no_record"
    assert derive_state(session, "pay_x") == PaymentState.ABANDONED
    last_event = history(session, "pay_x")[-1]
    assert last_event.abandon_reason == AbandonReason.EXECUTION_ERROR.value
    assert last_event.payload["source"] == "reconciliation_poll"


def test_stuck_awaiting_confirmation_with_paid_remote_status_reconciles_to_recovered():
    session = _session()
    client = FakeRazorpayClient()
    now = datetime(2026, 1, 1)
    idem_key, _ = _schedule_and_create_link(session, client, "pay_x", now)
    append_event(session, "pay_x", PaymentState.EXECUTING, now)
    append_event(session, "pay_x", PaymentState.AWAITING_CONFIRMATION, now)
    session.commit()
    client.set_status(idem_key, "paid")  # webhook never arrived, but it did get paid

    poll_time = now + timedelta(minutes=STALENESS + 1)
    results = run_reconciliation_poll_once(session, client, poll_time, staleness_minutes=STALENESS)

    assert results[0].action == "reconciled_to_recovered"
    assert derive_state(session, "pay_x") == PaymentState.RECOVERED
    # No spurious extra AWAITING_CONFIRMATION event -- it was already there.
    events = history(session, "pay_x")
    assert [e.to_state for e in events] == [
        "at_risk", "diagnosed", "scheduled", "executing", "awaiting_confirmation", "recovered",
    ]


def test_stuck_awaiting_confirmation_with_expired_remote_status_abandons_with_payment_failed():
    session = _session()
    client = FakeRazorpayClient()
    now = datetime(2026, 1, 1)
    idem_key, _ = _schedule_and_create_link(session, client, "pay_x", now)
    append_event(session, "pay_x", PaymentState.EXECUTING, now)
    append_event(session, "pay_x", PaymentState.AWAITING_CONFIRMATION, now)
    session.commit()
    client.set_status(idem_key, "expired")

    poll_time = now + timedelta(minutes=STALENESS + 1)
    results = run_reconciliation_poll_once(session, client, poll_time, staleness_minutes=STALENESS)

    assert results[0].action == "reconciled_to_abandoned"
    assert derive_state(session, "pay_x") == PaymentState.ABANDONED
    last_event = history(session, "pay_x")[-1]
    assert last_event.abandon_reason == AbandonReason.PAYMENT_FAILED.value
    assert last_event.payload["source"] == "reconciliation_poll"


def test_not_yet_stale_payment_is_left_untouched():
    session = _session()
    client = FakeRazorpayClient()
    now = datetime(2026, 1, 1)
    idem_key, _ = _schedule_and_create_link(session, client, "pay_x", now)
    append_event(session, "pay_x", PaymentState.EXECUTING, now)
    session.commit()

    # Only 5 minutes have passed -- well within the staleness threshold.
    poll_time = now + timedelta(minutes=5)
    results = run_reconciliation_poll_once(session, client, poll_time, staleness_minutes=STALENESS)

    assert results == []
    assert derive_state(session, "pay_x") == PaymentState.EXECUTING


def test_simulated_sent_never_appears_in_the_pollers_stuck_set():
    """2026-08-25 fix: a simulated action (outbox.py's
    SIMULATED_ACTION_TYPES) reaches the terminal SIMULATED_SENT, never
    AWAITING_CONFIRMATION. The poller must never even consider it, let
    alone misclassify it as execution_error -- querying Razorpay for a
    reference_id that was never actually sent would (correctly) find
    nothing, and "correctly find nothing" is not the same as "the system
    failed", which is exactly the false-statement-in-the-ledger problem
    this state exists to avoid."""
    session = _session()
    client = FakeRazorpayClient()
    now = datetime(2026, 1, 1)
    _to_diagnosed(session, "pay_x", now)
    idem_key = make_idempotency_key("pay_x", 1)
    write_intent_with_state_change(
        session, "pay_x", idem_key, "retry_now",
        {"amount_paise": 100_000, "customer_name": "C", "customer_contact": "9000000000"}, now,
    )
    append_event(session, "pay_x", PaymentState.EXECUTING, now)
    append_event(
        session, "pay_x", PaymentState.SIMULATED_SENT, now,
        payload={"simulated": True, "action_type": "retry_now", "note": "test fixture"},
    )
    session.commit()
    assert derive_state(session, "pay_x") == PaymentState.SIMULATED_SENT

    # Long past any staleness threshold -- if SIMULATED_SENT were treated
    # like EXECUTING/AWAITING_CONFIRMATION, this would already have fired.
    poll_time = now + timedelta(days=30)
    results = run_reconciliation_poll_once(session, client, poll_time, staleness_minutes=STALENESS)

    # results == [] proves _stale_payment_ids never selected this payment at
    # all -- the loop body (which is the only place fetch_payment_link_status
    # gets called) never ran for it, not just that nothing changed.
    assert results == []
    assert derive_state(session, "pay_x") == PaymentState.SIMULATED_SENT  # unchanged


def test_stuck_awaiting_confirmation_still_pending_makes_no_event_and_no_state_change():
    session = _session()
    client = FakeRazorpayClient()
    now = datetime(2026, 1, 1)
    idem_key, _ = _schedule_and_create_link(session, client, "pay_x", now)
    append_event(session, "pay_x", PaymentState.EXECUTING, now)
    append_event(session, "pay_x", PaymentState.AWAITING_CONFIRMATION, now)
    session.commit()
    # remote status stays "created" -- genuinely still waiting on the customer.

    poll_time = now + timedelta(minutes=STALENESS + 1)
    before_count = len(history(session, "pay_x"))
    results = run_reconciliation_poll_once(session, client, poll_time, staleness_minutes=STALENESS)

    assert results[0].action == "still_pending"
    assert results[0].new_state is None
    assert derive_state(session, "pay_x") == PaymentState.AWAITING_CONFIRMATION
    assert len(history(session, "pay_x")) == before_count  # no event appended
