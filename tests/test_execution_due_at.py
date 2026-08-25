"""due_at: the worker respects a decision's scheduled offset instead of
firing every intent immediately (2026-08-25 audit fix #2). FakeRazorpayClient
and an injected clock throughout -- no network, no real time reads."""

from datetime import datetime, timedelta

from execution.db import make_engine, make_session_factory
from execution.eventlog import append_event, derive_state
from execution.idempotency import make_idempotency_key
from execution.orchestrator import diagnose_and_schedule
from execution.outbox import run_outbox_worker_once, write_intent_with_state_change
from execution.razorpay_client import FakeRazorpayClient
from execution.states import PaymentState
from simulator.full_agent import make_full_agent_policy
from simulator.policies import RULES_ONLY_TABLE
from simulator.types import Customer, DeclineReason, InstrumentType, Language, Payment

WINDOW_START = datetime(2026, 1, 1, 10, 0)

PERSONA = Customer(
    customer_id="cust_due_at",
    recovery_propensity=0.6,
    funds_arrival_day=2,
    contact_hours=(0, 24),
    annoyance_threshold=10,
    channel_response_rate={"upi_link": 0.7, "whatsapp": 0.6, "sms": 0.5},
    preferred_language=Language.EN,
)


def _session():
    engine = make_engine("sqlite:///:memory:")
    return make_session_factory(engine)()


def _to_diagnosed(session, payment_id, now):
    from execution.states import PaymentState as PS

    append_event(session, payment_id, PS.AT_RISK, now)
    append_event(session, payment_id, PS.DIAGNOSED, now)
    session.commit()


def test_intent_with_due_at_in_the_future_is_not_picked_up():
    session = _session()
    now = datetime(2026, 1, 1, 10, 0)
    _to_diagnosed(session, "pay_x", now)
    write_intent_with_state_change(
        session,
        "pay_x",
        make_idempotency_key("pay_x", 1),
        "send_payment_link",
        {"amount_paise": 100_000, "customer_name": "C", "customer_contact": "9000000000"},
        now,
        due_at=now + timedelta(hours=4),
    )
    client = FakeRazorpayClient()

    # now, now+1hr, now+3h59m -- all still before due_at.
    for elapsed in (timedelta(0), timedelta(hours=1), timedelta(hours=3, minutes=59)):
        processed = run_outbox_worker_once(session, client, now + elapsed)
        assert processed == [], elapsed

    assert client.calls == []
    assert derive_state(session, "pay_x") == PaymentState.SCHEDULED  # never advanced


def test_same_intent_is_picked_up_once_now_passes_due_at():
    session = _session()
    now = datetime(2026, 1, 1, 10, 0)
    _to_diagnosed(session, "pay_x", now)
    due_at = now + timedelta(hours=4)
    write_intent_with_state_change(
        session,
        "pay_x",
        make_idempotency_key("pay_x", 1),
        "send_payment_link",
        {"amount_paise": 100_000, "customer_name": "C", "customer_contact": "9000000000"},
        now,
        due_at=due_at,
    )
    client = FakeRazorpayClient()

    # Not due yet at exactly one second before due_at.
    assert run_outbox_worker_once(session, client, due_at - timedelta(seconds=1)) == []

    # Due exactly AT due_at (<=, not <).
    processed = run_outbox_worker_once(session, client, due_at)
    assert len(processed) == 1
    assert processed[0].status == "done"
    assert len(client.calls) == 1
    assert derive_state(session, "pay_x") == PaymentState.AWAITING_CONFIRMATION


def test_default_due_at_is_event_time_for_backward_compatible_callers():
    """Callers that never pass due_at (every pre-existing one) keep behaving
    exactly as before: due immediately."""
    session = _session()
    now = datetime(2026, 1, 1, 10, 0)
    _to_diagnosed(session, "pay_x", now)
    write_intent_with_state_change(
        session,
        "pay_x",
        make_idempotency_key("pay_x", 1),
        "send_payment_link",
        {"amount_paise": 100_000, "customer_name": "C", "customer_contact": "9000000000"},
        now,
        # no due_at passed
    )
    client = FakeRazorpayClient()
    processed = run_outbox_worker_once(session, client, now)
    assert len(processed) == 1


def test_full_agents_scheduled_decisions_produce_correct_due_at_values():
    """orchestrator.diagnose_and_schedule, driven by full_agent instead of
    rules_only, sets due_at = now + the offset decide() actually chose."""
    factory = make_full_agent_policy(failure_log=[], outage_events=[])

    # INSUFFICIENT_FUNDS -> full_agent's SEND_PAYMENT_LINK candidate offset is 1 hour.
    session = _session()
    now = WINDOW_START
    payment = Payment(
        payment_id="pay_full_agent_1",
        customer_id=PERSONA.customer_id,
        amount_paise=500_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=DeclineReason.INSUFFICIENT_FUNDS,
        issuer_code="HDFC",
        failed_at=now,
    )
    from simulator import ContactTracker

    policy_fn = factory(ContactTracker())
    intent = diagnose_and_schedule(session, payment, PERSONA, now, policy_fn=policy_fn)
    assert intent is not None
    assert intent.due_at == now + timedelta(hours=1)

    # NETWORK_TIMEOUT -> full_agent's RETRY_NOW candidate offset is 2 minutes.
    session2 = _session()
    payment2 = Payment(
        payment_id="pay_full_agent_2",
        customer_id=PERSONA.customer_id,
        amount_paise=500_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=DeclineReason.NETWORK_TIMEOUT,
        issuer_code="HDFC",
        failed_at=now,
    )
    policy_fn2 = factory(ContactTracker())
    intent2 = diagnose_and_schedule(session2, payment2, PERSONA, now, policy_fn=policy_fn2)
    assert intent2 is not None
    assert intent2.due_at == now + timedelta(minutes=2)


def test_rules_only_table_offsets_are_exactly_what_orchestrator_used_above():
    """Cross-check against the source of truth table itself, so this test
    suite doesn't silently drift from RULES_ONLY_TABLE if it changes."""
    offset, action_type, _channel = RULES_ONLY_TABLE[DeclineReason.INSUFFICIENT_FUNDS]
    assert offset == timedelta(hours=1)
    offset2, action_type2, _channel2 = RULES_ONLY_TABLE[DeclineReason.NETWORK_TIMEOUT]
    assert offset2 == timedelta(minutes=2)
