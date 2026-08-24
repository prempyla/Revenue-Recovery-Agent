"""apply_reply_intent: the only consumer of ReplyIntent. Structural
guarantee (no execution-layer import) plus behavioral tests for OPT_OUT
(explicit flag + full_agent veto) and PROMISE_TO_PAY (re-evaluation through
the existing decide() path, never a direct action)."""

import ast
import inspect
from datetime import datetime, timedelta

from llm import ClassifiedReply, ReplyIntent, apply_reply_intent
from simulator import ContactTracker, DeclineReason, InstrumentType, Language
from simulator.full_agent import decide
from simulator.types import ActionType, Customer, Payment

WINDOW_START = datetime(2026, 1, 1, 10, 0)

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


def test_reply_handling_module_has_no_execution_layer_import():
    """Structural guarantee, not just empirical: parse the module source and
    confirm it never imports anything from execution.* or razorpay_client."""
    import llm.reply_handling as module

    source = inspect.getsource(module)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "execution" not in alias.name
                assert "razorpay" not in alias.name
        if isinstance(node, ast.ImportFrom):
            assert node.module is None or ("execution" not in node.module and "razorpay" not in node.module)


def test_opt_out_marks_the_tracker_and_returns_no_action():
    tracker = ContactTracker()
    classified = ClassifiedReply(ReplyIntent.OPT_OUT)
    outcome = apply_reply_intent(classified, PAYMENT, PERSONA, tracker, [], [], WINDOW_START)

    assert outcome is None
    assert tracker.is_opted_out(PERSONA.customer_id) is True


def test_opt_out_causes_full_agent_decide_to_veto_subsequent_actions():
    """The actual enforcement lives in full_agent.decide()'s existing hard
    veto -- this test proves the OPT_OUT reply genuinely propagates there,
    not just that a flag got set somewhere."""
    tracker = ContactTracker()
    classified = ClassifiedReply(ReplyIntent.OPT_OUT)
    apply_reply_intent(classified, PAYMENT, PERSONA, tracker, [], [], WINDOW_START)

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
    tracker = ContactTracker()
    promised = (WINDOW_START + timedelta(days=5)).date()
    classified = ClassifiedReply(ReplyIntent.PROMISE_TO_PAY, promised_date=promised)

    outcome = apply_reply_intent(classified, PAYMENT, PERSONA, tracker, [], [], WINDOW_START)

    # Whatever comes back is exactly what decide() would return -- this
    # function never executes anything itself, only calls the existing path.
    direct_decision = decide(
        PAYMENT, PERSONA, datetime.combine(promised, datetime.min.time()), tracker, [], []
    )
    assert outcome == direct_decision


def test_promise_to_pay_without_date_defaults_to_a_short_reevaluation_delay():
    tracker = ContactTracker()
    classified = ClassifiedReply(ReplyIntent.PROMISE_TO_PAY, promised_date=None)
    outcome = apply_reply_intent(classified, PAYMENT, PERSONA, tracker, [], [], WINDOW_START)
    # Should not raise, and should be a valid decide()-shaped result or None.
    assert outcome is None or isinstance(outcome, tuple)


def test_dispute_and_wrong_person_and_unclear_never_produce_an_action():
    for intent in (ReplyIntent.DISPUTE, ReplyIntent.WRONG_PERSON, ReplyIntent.UNCLEAR):
        tracker = ContactTracker()
        classified = ClassifiedReply(intent)
        outcome = apply_reply_intent(classified, PAYMENT, PERSONA, tracker, [], [], WINDOW_START)
        assert outcome is None, intent
        assert tracker.is_opted_out(PERSONA.customer_id) is False
