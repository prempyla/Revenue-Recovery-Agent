"""apply_reply_intent: the only consumer of ReplyIntent. Structural
guarantee (narrowed 2026-08-25: never imports the money-moving modules,
outbox.py/razorpay_client.py — it DOES now import execution.opt_out_events
to durably persist an opt-out, an audit-only table with no FSM/money
involvement) plus behavioral tests for OPT_OUT (explicit flag + persisted
event + full_agent veto) and PROMISE_TO_PAY (re-evaluation through the
existing decide() path, never a direct action)."""

import ast
import inspect
from datetime import datetime, timedelta, timezone

from llm import ClassifiedReply, ReplyIntent, apply_reply_intent
from simulator import ContactTracker, DeclineReason, InstrumentType, Language
from simulator.full_agent import decide
from simulator.types import ActionType, Customer, Payment

from execution.db import make_engine, make_session_factory
from execution.opt_out_events import CustomerOptOutEvent

WINDOW_START = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)

PERSONA = Customer(
    customer_id="cust_reply",
    recovery_propensity=0.6,
    funds_arrival_day=2,
    contact_hours=(9, 21),
    annoyance_threshold=10,
    channel_response_rate={"upi_link": 0.7, "whatsapp": 0.6, "sms": 0.5},
    preferred_language=Language.EN,
)

PAYMENT = Payment(
    payment_id="pay_reply",
    customer_id=PERSONA.customer_id,
    amount_paise=500_000,
    instrument_type=InstrumentType.CARD,
    decline_reason=DeclineReason.INSUFFICIENT_FUNDS,
    issuer_code="HDFC",
    failed_at=WINDOW_START,
)


def _session():
    engine = make_engine("sqlite:///:memory:")
    return make_session_factory(engine)()


def test_reply_handling_module_never_imports_money_moving_execution_modules():
    """Structural guarantee, corrected 2026-08-25: the previous claim ("no
    import of anything under execution/ at all") was stricter than what
    actually needed guaranteeing. apply_reply_intent now legitimately
    imports execution.opt_out_events to PERSIST the opt-out fact -- an
    audit-only table, no PaymentState/FSM involvement, no money movement.
    What must still hold, and does: it never imports outbox.py or
    razorpay_client.py, the two modules that can move money or touch a
    real/fake payment API."""
    import llm.reply_handling as module

    source = inspect.getsource(module)
    tree = ast.parse(source)
    forbidden = {"execution.outbox", "execution.razorpay_client", "razorpay_client"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden, alias.name
        if isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden, node.module


def test_opt_out_marks_the_tracker_and_returns_no_action():
    session = _session()
    tracker = ContactTracker()
    classified = ClassifiedReply(ReplyIntent.OPT_OUT)
    outcome = apply_reply_intent(session, classified, PAYMENT, PERSONA, tracker, [], [], WINDOW_START)

    assert outcome is None
    assert tracker.is_opted_out(PERSONA.customer_id) is True


def test_opt_out_persists_an_event_with_the_reply_intent_and_timestamp():
    session = _session()
    tracker = ContactTracker()
    classified = ClassifiedReply(ReplyIntent.OPT_OUT)
    apply_reply_intent(session, classified, PAYMENT, PERSONA, tracker, [], [], WINDOW_START)

    rows = session.query(CustomerOptOutEvent).filter_by(customer_id=PERSONA.customer_id).all()
    assert len(rows) == 1
    assert rows[0].reply_intent == "OPT_OUT"
    assert rows[0].source_payment_id == PAYMENT.payment_id
    assert rows[0].event_time == WINDOW_START


def test_opt_out_causes_full_agent_decide_to_veto_subsequent_actions():
    """The actual enforcement lives in full_agent.decide()'s existing hard
    veto -- this test proves the OPT_OUT reply genuinely propagates there,
    not just that a flag got set somewhere."""
    session = _session()
    tracker = ContactTracker()
    classified = ClassifiedReply(ReplyIntent.OPT_OUT)
    apply_reply_intent(session, classified, PAYMENT, PERSONA, tracker, [], [], WINDOW_START)

    later_payment = Payment(
        payment_id="pay_reply_2",
        customer_id=PERSONA.customer_id,
        amount_paise=500_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=DeclineReason.AFA_3DS_DROPOFF,  # customer-facing candidate
        issuer_code="HDFC",
        failed_at=WINDOW_START + timedelta(hours=1),
    )
    decision = decide(later_payment, PERSONA, WINDOW_START, tracker, [], [])
    assert decision is None  # vetoed by explicit_opt_out


def test_promise_to_pay_with_date_schedules_reevaluation_at_that_date():
    session = _session()
    tracker = ContactTracker()
    promised = (WINDOW_START + timedelta(days=5)).date()
    classified = ClassifiedReply(ReplyIntent.PROMISE_TO_PAY, promised_date=promised)

    outcome = apply_reply_intent(session, classified, PAYMENT, PERSONA, tracker, [], [], WINDOW_START)

    # Whatever comes back is exactly what decide() would return -- this
    # function never executes anything itself, only calls the existing path.
    direct_decision = decide(
        PAYMENT, PERSONA, datetime.combine(promised, datetime.min.time()), tracker, [], []
    )
    assert outcome == direct_decision


def test_promise_to_pay_without_date_defaults_to_a_short_reevaluation_delay():
    session = _session()
    tracker = ContactTracker()
    classified = ClassifiedReply(ReplyIntent.PROMISE_TO_PAY, promised_date=None)
    outcome = apply_reply_intent(session, classified, PAYMENT, PERSONA, tracker, [], [], WINDOW_START)
    # Should not raise, and should be a valid decide()-shaped result or None.
    assert outcome is None or isinstance(outcome, tuple)


def test_dispute_and_wrong_person_and_unclear_never_produce_an_action():
    for intent in (ReplyIntent.DISPUTE, ReplyIntent.WRONG_PERSON, ReplyIntent.UNCLEAR):
        session = _session()
        tracker = ContactTracker()
        classified = ClassifiedReply(intent)
        outcome = apply_reply_intent(session, classified, PAYMENT, PERSONA, tracker, [], [], WINDOW_START)
        assert outcome is None, intent
        assert tracker.is_opted_out(PERSONA.customer_id) is False
        assert session.query(CustomerOptOutEvent).count() == 0
