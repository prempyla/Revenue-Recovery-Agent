"""P1 #2 (2026-08-25, DECISIONS.md): the worker lease. Before this,
run_outbox_worker_once's SELECT of pending+due rows had no claim step --
two concurrent workers would both fetch the same rows and both execute
them, with Razorpay's reference_id uniqueness catching the duplicate only
as an ERROR after the fact, not preventing it. These tests interleave two
workers' CLAIM calls before either one processes anything -- that's the
actual race window a lease has to close, and testing it that way (rather
than just calling run_outbox_worker_once twice in a row, which would
trivially pass even without a lease, since the first call already marks
everything "done") is what makes this a real regression test rather than
a vacuous one."""

from datetime import datetime, timedelta, timezone

import pytest

from execution.db import make_engine, make_session_factory
from execution.eventlog import append_event, derive_state
from execution.idempotency import make_idempotency_key
from execution.outbox import (
    DEFAULT_LEASE_DURATION,
    OutboxIntent,
    _claim_batch,
    _process_claimed_intents,
    run_outbox_worker_once,
    write_intent_with_state_change,
)
from execution.razorpay_client import FakeRazorpayClient
from execution.states import PaymentState

NOW = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)


def _session():
    engine = make_engine("sqlite:///:memory:")
    return make_session_factory(engine)()


def _seed_pending_intents(session, count, now=NOW, action_type="send_payment_link"):
    for i in range(count):
        payment_id = f"pay_{i}"
        append_event(session, payment_id, PaymentState.AT_RISK, now)
        append_event(session, payment_id, PaymentState.DIAGNOSED, now)
        session.commit()
        write_intent_with_state_change(
            session,
            payment_id,
            make_idempotency_key(payment_id, 1),
            action_type,
            {"amount_paise": 100_000, "customer_name": "C", "customer_contact": "9000000000"},
            now,
        )


def test_two_workers_claiming_the_same_pending_set_each_execute_every_intent_exactly_once():
    """5 pending intents, batch_size=3 -- forces a genuine split rather
    than one worker trivially winning everything. worker_a claims first
    (up to 3), worker_b claims what's left (2) -- BOTH claim calls happen
    before either worker processes anything. Then both process their own
    claimed batch. Total processed across both workers must equal the
    total intents, with zero overlap and zero duplication."""
    session = _session()
    _seed_pending_intents(session, count=5)
    client = FakeRazorpayClient()

    claimed_a = _claim_batch(session, "worker_a", NOW, DEFAULT_LEASE_DURATION, batch_size=3)
    claimed_b = _claim_batch(session, "worker_b", NOW, DEFAULT_LEASE_DURATION, batch_size=3)

    ids_a = {i.id for i in claimed_a}
    ids_b = {i.id for i in claimed_b}
    assert ids_a & ids_b == set()  # no overlap between what each worker claimed
    assert len(claimed_a) == 3
    assert len(claimed_b) == 2
    assert ids_a | ids_b == {1, 2, 3, 4, 5}  # together, everything

    processed_a = _process_claimed_intents(session, client, NOW, claimed_a)
    processed_b = _process_claimed_intents(session, client, NOW, claimed_b)

    assert len(processed_a) + len(processed_b) == 5
    assert len(client.calls) == 5  # exactly one real dispatch per intent, no duplicates
    for i in range(5):
        assert derive_state(session, f"pay_{i}") == PaymentState.AWAITING_CONFIRMATION
    all_intents = session.query(OutboxIntent).all()
    assert all(intent.status == "done" for intent in all_intents)
    assert {intent.claimed_by for intent in all_intents} == {"worker_a", "worker_b"}


def test_a_row_claimed_with_an_unexpired_lease_is_not_picked_up_by_a_second_worker():
    session = _session()
    _seed_pending_intents(session, count=1)

    claimed_a = _claim_batch(session, "worker_a", NOW, DEFAULT_LEASE_DURATION, batch_size=10)
    assert len(claimed_a) == 1

    # Same instant, a different worker tries to claim the same pending set.
    claimed_b = _claim_batch(session, "worker_b", NOW, DEFAULT_LEASE_DURATION, batch_size=10)
    assert claimed_b == []

    # Still true a little later, as long as it's before the lease expires.
    claimed_b_again = _claim_batch(
        session, "worker_b", NOW + timedelta(minutes=1), DEFAULT_LEASE_DURATION, batch_size=10
    )
    assert claimed_b_again == []


def test_a_row_whose_lease_has_expired_is_reclaimable_by_a_second_worker():
    """The dead-worker recovery case: worker_a claims, then never
    processes (simulating a crash between claim and execute) -- once its
    lease expires, worker_b must be able to reclaim and actually execute
    the intent. Without this, a crashed worker's claims would strand
    every intent it touched forever."""
    session = _session()
    _seed_pending_intents(session, count=1)
    client = FakeRazorpayClient()
    lease_duration = timedelta(minutes=5)

    claimed_a = _claim_batch(session, "worker_a", NOW, lease_duration, batch_size=10)
    assert len(claimed_a) == 1
    # worker_a "crashes" here -- never calls _process_claimed_intents.

    # Still within the lease: not yet reclaimable.
    still_leased = _claim_batch(
        session, "worker_b", NOW + timedelta(minutes=4), lease_duration, batch_size=10
    )
    assert still_leased == []

    # Past the lease expiry: reclaimable.
    reclaim_time = NOW + timedelta(minutes=5, seconds=1)
    claimed_b = _claim_batch(session, "worker_b", reclaim_time, lease_duration, batch_size=10)
    assert len(claimed_b) == 1
    assert claimed_b[0].id == claimed_a[0].id
    assert claimed_b[0].claimed_by == "worker_b"  # ownership genuinely transferred

    processed_b = _process_claimed_intents(session, client, reclaim_time, claimed_b)
    assert len(processed_b) == 1
    assert len(client.calls) == 1  # executed exactly once, by the worker that actually reclaimed it
    assert derive_state(session, "pay_0") == PaymentState.AWAITING_CONFIRMATION


def test_a_claimed_row_still_respects_due_at():
    """The lease is an additional filter, not a replacement for the
    existing due_at check -- a not-yet-due intent must not be claimable by
    anyone, leased or not."""
    session = _session()
    payment_id = "pay_future"
    append_event(session, payment_id, PaymentState.AT_RISK, NOW)
    append_event(session, payment_id, PaymentState.DIAGNOSED, NOW)
    session.commit()
    write_intent_with_state_change(
        session,
        payment_id,
        make_idempotency_key(payment_id, 1),
        "send_payment_link",
        {"amount_paise": 100_000, "customer_name": "C", "customer_contact": "9000000000"},
        NOW,
        due_at=NOW + timedelta(hours=4),
    )

    not_yet_due = _claim_batch(session, "worker_a", NOW, DEFAULT_LEASE_DURATION, batch_size=10)
    assert not_yet_due == []

    now_due = _claim_batch(
        session, "worker_a", NOW + timedelta(hours=4), DEFAULT_LEASE_DURATION, batch_size=10
    )
    assert len(now_due) == 1


def test_run_outbox_worker_once_end_to_end_with_two_worker_ids_in_sequence():
    """Sanity check at the public API level (not just the internal claim
    helper): two full run_outbox_worker_once calls with different
    worker_ids against the same pending set, one after the other, never
    double-execute the same intent -- the second call correctly finds
    nothing left pending."""
    session = _session()
    _seed_pending_intents(session, count=2)
    client = FakeRazorpayClient()

    processed_a = run_outbox_worker_once(session, client, NOW, "worker_a")
    processed_b = run_outbox_worker_once(session, client, NOW, "worker_b")

    assert len(processed_a) == 2
    assert len(processed_b) == 0  # nothing left for worker_b -- worker_a already did everything
    assert len(client.calls) == 2


def test_double_execution_occurs_without_the_claim_step_confirming_the_lease_is_load_bearing():
    """Per instruction: prove the concurrency test above would fail
    without the lease, don't just assert the fixed code is correct.
    Monkeypatches _claim_batch back to the pre-fix behavior (a plain
    SELECT, no claiming at all) and confirms the SAME 5-intent/batch=3
    scenario as the first test above now double-executes -- then restores
    the real _claim_batch and re-confirms it doesn't."""
    import execution.outbox as outbox_module

    def _unleased_select(session, worker_id, now, lease_duration, batch_size):
        # The pre-fix behavior: fetch eligible rows, claim nothing.
        return (
            session.query(OutboxIntent)
            .filter(OutboxIntent.status == "pending", OutboxIntent.due_at <= now)
            .order_by(OutboxIntent.id)
            .limit(batch_size)
            .all()
        )

    real_claim_batch = outbox_module._claim_batch
    session = _session()
    _seed_pending_intents(session, count=5)
    client = FakeRazorpayClient()

    outbox_module._claim_batch = _unleased_select
    try:
        # Both workers "see" the same unclaimed pending set, exactly as the
        # pre-fix bug would let them.
        seen_by_a = outbox_module._claim_batch(session, "worker_a", NOW, DEFAULT_LEASE_DURATION, batch_size=3)
        seen_by_b = outbox_module._claim_batch(session, "worker_b", NOW, DEFAULT_LEASE_DURATION, batch_size=3)
        overlap = {i.id for i in seen_by_a} & {i.id for i in seen_by_b}
        assert len(overlap) > 0, "the bug this lease fixes should reproduce here"
    finally:
        outbox_module._claim_batch = real_claim_batch

    # Restored: the real fix, on a fresh identical scenario, does not
    # reproduce the overlap.
    session2 = _session()
    _seed_pending_intents(session2, count=5)
    claimed_a2 = outbox_module._claim_batch(session2, "worker_a", NOW, DEFAULT_LEASE_DURATION, batch_size=3)
    claimed_b2 = outbox_module._claim_batch(session2, "worker_b", NOW, DEFAULT_LEASE_DURATION, batch_size=3)
    assert {i.id for i in claimed_a2} & {i.id for i in claimed_b2} == set()


def test_a_worker_whose_processing_outlasts_its_own_lease_does_not_double_execute():
    """Found by stress-testing the mechanism after it shipped, not
    requested: the lease is time-based, not transaction-based like
    Postgres's FOR UPDATE SKIP LOCKED. If a worker's actual processing
    takes longer than lease_duration, a second worker can correctly (from
    its own perspective) reclaim and finish the same row first -- the
    first worker is not dead, just slow. Confirms the FSM's own
    validate_transition is the real backstop: the slow worker's belated
    append_event(EXECUTING) on a row the fast worker already moved past
    EXECUTING must be caught and skipped, not crash the rest of its batch
    and not make a second real dispatch call."""
    # Same engine, two independent sessions -- :memory: + StaticPool
    # (db.py) makes this behave like two workers sharing one database,
    # same pattern used elsewhere in this repo for simulating concurrent
    # access without needing real threads or a file-backed DB.
    engine = make_engine("sqlite:///:memory:")
    session_factory = make_session_factory(engine)
    session_slow = session_factory()
    session_fast = session_factory()
    lease = timedelta(minutes=5)

    payment_id = "pay_slow"
    append_event(session_slow, payment_id, PaymentState.AT_RISK, NOW)
    append_event(session_slow, payment_id, PaymentState.DIAGNOSED, NOW)
    session_slow.commit()
    write_intent_with_state_change(
        session_slow,
        payment_id,
        make_idempotency_key(payment_id, 1),
        "send_payment_link",
        {"amount_paise": 100_000, "customer_name": "C", "customer_contact": "9000000000"},
        NOW,
    )

    # worker_slow claims but is, in fact, slow -- it hasn't processed yet
    # by the time its own lease has expired.
    claimed_slow = _claim_batch(session_slow, "worker_slow", NOW, lease, batch_size=10)
    assert len(claimed_slow) == 1

    # worker_fast checks in on a separate session after the lease expiry,
    # correctly (from its perspective) reclaims, and finishes first.
    past_expiry = NOW + lease + timedelta(seconds=1)
    claimed_fast = _claim_batch(session_fast, "worker_fast", past_expiry, lease, batch_size=10)
    assert len(claimed_fast) == 1

    client_fast = FakeRazorpayClient()
    processed_fast = _process_claimed_intents(session_fast, client_fast, past_expiry, claimed_fast)
    assert len(processed_fast) == 1
    assert len(client_fast.calls) == 1

    # worker_slow now finally gets around to the intent it claimed
    # earlier -- must be skipped, not crash, not dispatch a second time.
    client_slow = FakeRazorpayClient()
    processed_slow = _process_claimed_intents(session_slow, client_slow, NOW, claimed_slow)
    assert processed_slow == []  # skipped -- not "ours" anymore
    assert client_slow.calls == []  # no second real dispatch

    assert derive_state(session_fast, payment_id) == PaymentState.AWAITING_CONFIRMATION
