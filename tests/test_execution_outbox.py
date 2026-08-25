"""Isolation tests for the outbox: crash survival, idempotency-key
determinism, unmapped-action NotImplementedError, execution-error handling."""

from datetime import datetime

import pytest

from execution.db import make_engine, make_session_factory
from execution.eventlog import append_event, derive_state, history
from execution.idempotency import make_idempotency_key
from execution.outbox import OutboxIntent, run_outbox_worker_once, write_intent_with_state_change
from execution.razorpay_client import FakeRazorpayClient
from execution.states import AbandonReason, PaymentState


def _session():
    engine = make_engine("sqlite:///:memory:")
    return make_session_factory(engine)()


def _to_diagnosed(session, payment_id, now):
    append_event(session, payment_id, PaymentState.AT_RISK, now)
    append_event(session, payment_id, PaymentState.DIAGNOSED, now)
    session.commit()


def test_idempotency_key_is_deterministic_across_separate_calls():
    key_a = make_idempotency_key("pay_x", 1)
    key_b = make_idempotency_key("pay_x", 1)
    assert key_a == key_b
    assert key_a == "pay_x:attempt:1"


def test_idempotency_key_differs_by_attempt_number():
    assert make_idempotency_key("pay_x", 1) != make_idempotency_key("pay_x", 2)


def test_intent_survives_a_simulated_crash_between_persist_and_execute(tmp_path):
    """Write the intent through one engine/session, then simulate a process
    restart by opening a BRAND NEW engine against the same on-disk file
    (:memory: wouldn't survive this) -- confirm the pending intent is still
    there and gets picked up and executed correctly by a fresh worker call."""
    db_path = tmp_path / "execution.db"
    db_url = f"sqlite:///{db_path}"
    now = datetime(2026, 1, 1)

    # "Process 1": decide and persist, then simulate a crash (just stop
    # using this engine/session -- nothing further happens with it).
    engine_1 = make_engine(db_url)
    session_1 = make_session_factory(engine_1)()
    _to_diagnosed(session_1, "pay_x", now)
    write_intent_with_state_change(
        session_1,
        "pay_x",
        make_idempotency_key("pay_x", 1),
        "send_payment_link",
        {"amount_paise": 100_000, "customer_name": "C", "customer_contact": "9000000000"},
        now,
    )
    session_1.close()

    # "Process 2": a fresh engine/session reconnecting to the same file.
    engine_2 = make_engine(db_url)
    session_2 = make_session_factory(engine_2)()

    pending = session_2.query(OutboxIntent).filter_by(status="pending").all()
    assert len(pending) == 1
    assert derive_state(session_2, "pay_x") == PaymentState.SCHEDULED

    client = FakeRazorpayClient()
    processed = run_outbox_worker_once(session_2, client, now)

    assert len(processed) == 1
    assert processed[0].status == "done"
    assert len(client.calls) == 1
    assert derive_state(session_2, "pay_x") == PaymentState.AWAITING_CONFIRMATION


def test_unmapped_action_type_raises_not_implemented():
    """2026-08-25: retry_now (the previous example here) is now dispatched
    as a logged simulated send -- see SIMULATED_ACTION_TYPES -- so it no
    longer demonstrates "unmapped". Using a genuinely fictional action_type
    instead; every real ActionType enum value is now covered by _DISPATCH."""
    session = _session()
    now = datetime(2026, 1, 1)
    _to_diagnosed(session, "pay_x", now)
    write_intent_with_state_change(
        session, "pay_x", make_idempotency_key("pay_x", 1), "teleport_customer", {}, now
    )
    client = FakeRazorpayClient()
    with pytest.raises(NotImplementedError):
        run_outbox_worker_once(session, client, now)


def test_simulated_action_types_are_dispatched_without_a_real_api_call():
    """2026-08-25 fix: action types with no real Razorpay primitive (e.g.
    retry_now) are executed as a logged simulated send, explicitly tagged,
    never a real create_payment_link-style call."""
    from execution.outbox import SIMULATED_ACTION_TYPES

    for action_type in SIMULATED_ACTION_TYPES:
        session = _session()
        now = datetime(2026, 1, 1)
        _to_diagnosed(session, "pay_x", now)
        write_intent_with_state_change(
            session, "pay_x", make_idempotency_key("pay_x", 1), action_type, {}, now
        )
        client = FakeRazorpayClient()

        processed = run_outbox_worker_once(session, client, now)

        assert client.calls == [], f"{action_type}: must not make a real API call"
        assert processed[0].status == "done"
        assert processed[0].result["simulated"] is True
        assert processed[0].result["action_type"] == action_type
        # 2026-08-25: terminal SIMULATED_SENT, not AWAITING_CONFIRMATION --
        # nothing can ever confirm a send that never happened.
        assert derive_state(session, "pay_x") == PaymentState.SIMULATED_SENT
        last_event = history(session, "pay_x")[-1]
        assert last_event.payload["simulated"] is True
        assert last_event.to_state == PaymentState.SIMULATED_SENT.value


def test_execution_error_marks_abandoned_with_execution_error_reason():
    session = _session()
    now = datetime(2026, 1, 1)
    _to_diagnosed(session, "pay_x", now)
    idem_key = make_idempotency_key("pay_x", 1)
    write_intent_with_state_change(
        session,
        "pay_x",
        idem_key,
        "send_payment_link",
        {"amount_paise": 100_000, "customer_name": "C", "customer_contact": "9000000000"},
        now,
    )

    client = FakeRazorpayClient()
    client._seen_reference_ids.add(idem_key)  # force the create call to raise

    processed = run_outbox_worker_once(session, client, now)

    assert processed[0].status == "failed"
    assert processed[0].error is not None
    assert derive_state(session, "pay_x") == PaymentState.ABANDONED

    from execution.eventlog import history

    last_event = history(session, "pay_x")[-1]
    assert last_event.abandon_reason == AbandonReason.EXECUTION_ERROR.value
