"""The real end-to-end path driven by full_agent instead of rules_only —
mirrors test_execution_end_to_end.py, covering two action types: a real
Razorpay dispatch (send_payment_link) and a logged-simulated one (retry_now).

Interface friction, reported per the task: full_agent's decide() needs
failure_log/outage_events/a ContactTracker that a plain
(payment, customer) -> Plan policy_fn signature doesn't carry.
orchestrator.diagnose_and_schedule stays agnostic (any Callable[[Payment,
Customer], Plan] works) -- the caller here pre-binds those via
make_full_agent_policy(...)(tracker) before passing the result in.
There's no persistent store under execution/ yet that would let a real
deployment source failure_log/outage_events for full_agent's systemic
detector from live data; this test constructs them explicitly (empty, since
no outage scenario is exercised here), same as the simulator's own tests do.
"""

from datetime import datetime, timedelta, timezone

from simulator import ContactTracker
from simulator.full_agent import make_full_agent_policy
from simulator.types import Customer, DeclineReason, InstrumentType, Language, Payment

from execution.db import make_engine, make_session_factory
from execution.eventlog import derive_state, history
from execution.orchestrator import diagnose_and_schedule
from execution.outbox import run_outbox_worker_once
from execution.razorpay_client import FakeRazorpayClient
from execution.states import PaymentState
from execution.webhook import process_webhook_event

PERSONA = Customer(
    customer_id="cust_fa_e2e",
    recovery_propensity=0.6,
    funds_arrival_day=2,
    contact_hours=(9, 21),
    annoyance_threshold=5,
    channel_response_rate={"upi_link": 0.7, "whatsapp": 0.6, "sms": 0.5},
    preferred_language=Language.EN,
)

WINDOW_START = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)


def _session():
    engine = make_engine("sqlite:///:memory:")
    return make_session_factory(engine)()


def test_full_agent_send_payment_link_path_ends_in_recovered_via_webhook():
    """Action type 1: SEND_PAYMENT_LINK -- real Razorpay dispatch, webhook-
    confirmed, matches test_execution_end_to_end.py's shape exactly, just
    driven by full_agent instead of rules_only."""
    payment = Payment(
        payment_id="pay_fa_link",
        customer_id=PERSONA.customer_id,
        amount_paise=500_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=DeclineReason.INSUFFICIENT_FUNDS,
        issuer_code="HDFC",
        failed_at=WINDOW_START,
    )
    session = _session()
    tracker = ContactTracker()
    policy_fn = make_full_agent_policy(failure_log=[], outage_events=[])(tracker)

    intent = diagnose_and_schedule(session, payment, PERSONA, WINDOW_START, policy_fn=policy_fn)
    assert intent is not None
    assert intent.action_type == "send_payment_link"
    assert intent.due_at == WINDOW_START + timedelta(hours=1)  # full_agent's INSUFFICIENT_FUNDS offset
    assert derive_state(session, "pay_fa_link") == PaymentState.SCHEDULED

    client = FakeRazorpayClient()
    processed = run_outbox_worker_once(session, client, intent.due_at, "worker_1")
    assert processed[0].status == "done"
    assert len(client.calls) == 1  # a real dispatch happened
    assert derive_state(session, "pay_fa_link") == PaymentState.AWAITING_CONFIRMATION

    webhook_payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {"reference_id": intent.idempotency_key, "id": processed[0].result["id"]}
            }
        },
    }
    process_webhook_event(session, webhook_payload, intent.due_at)

    assert derive_state(session, "pay_fa_link") == PaymentState.RECOVERED
    assert [e.to_state for e in history(session, "pay_fa_link")] == [
        "at_risk", "diagnosed", "scheduled", "executing", "awaiting_confirmation", "recovered",
    ]


def test_full_agent_retry_now_path_is_dispatched_as_a_logged_simulated_send():
    """Action type 2: NETWORK_TIMEOUT -> RETRY_NOW -- silent, no real
    Razorpay primitive, dispatched as a logged simulated send. No webhook
    step: there's no real payment link for one to ever arrive for."""
    payment = Payment(
        payment_id="pay_fa_retry",
        customer_id=PERSONA.customer_id,
        amount_paise=500_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=DeclineReason.NETWORK_TIMEOUT,
        issuer_code="HDFC",
        failed_at=WINDOW_START,
    )
    session = _session()
    tracker = ContactTracker()
    policy_fn = make_full_agent_policy(failure_log=[], outage_events=[])(tracker)

    intent = diagnose_and_schedule(session, payment, PERSONA, WINDOW_START, policy_fn=policy_fn)
    assert intent is not None
    assert intent.action_type == "retry_now"
    assert intent.due_at == WINDOW_START + timedelta(minutes=2)  # full_agent's NETWORK_TIMEOUT offset

    client = FakeRazorpayClient()
    processed = run_outbox_worker_once(session, client, intent.due_at, "worker_1")

    assert processed[0].status == "done"
    assert client.calls == []  # no real API call for a simulated action
    assert processed[0].result["simulated"] is True
    assert processed[0].result["action_type"] == "retry_now"
    # 2026-08-25: terminal SIMULATED_SENT, not AWAITING_CONFIRMATION -- see
    # states.py's docstring: nothing can ever confirm a send that never
    # happened, so this must not wait on a confirmation that will never
    # arrive (which reconciliation.py would eventually misclassify as
    # execution_error -- a false statement, not a true failure).
    assert derive_state(session, "pay_fa_retry") == PaymentState.SIMULATED_SENT
    last_event = history(session, "pay_fa_retry")[-1]
    assert last_event.payload["simulated"] is True
    assert last_event.to_state == PaymentState.SIMULATED_SENT.value


def test_both_action_types_share_the_same_customer_and_tracker_without_interference():
    """Sanity check that driving two different payments for the same
    customer through full_agent, sharing one ContactTracker, doesn't
    cross-contaminate -- e.g. the silent retry_now shouldn't count as a
    contact against the customer-facing send_payment_link's compliance
    checks."""
    tracker = ContactTracker()
    policy_fn = make_full_agent_policy(failure_log=[], outage_events=[])(tracker)

    link_payment = Payment(
        payment_id="pay_shared_link",
        customer_id=PERSONA.customer_id,
        amount_paise=500_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=DeclineReason.INSUFFICIENT_FUNDS,
        issuer_code="HDFC",
        failed_at=WINDOW_START,
    )
    retry_payment = Payment(
        payment_id="pay_shared_retry",
        customer_id=PERSONA.customer_id,
        amount_paise=500_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=DeclineReason.NETWORK_TIMEOUT,
        issuer_code="HDFC",
        failed_at=WINDOW_START,
    )

    session = _session()
    link_intent = diagnose_and_schedule(session, link_payment, PERSONA, WINDOW_START, policy_fn=policy_fn)
    retry_intent = diagnose_and_schedule(session, retry_payment, PERSONA, WINDOW_START, policy_fn=policy_fn)

    assert link_intent.action_type == "send_payment_link"
    assert retry_intent.action_type == "retry_now"
    # retry_now is silent -- shouldn't have incremented the tracker at
    # decision time (contact accounting happens at actual send time in a
    # full system; decide() itself doesn't record contacts).
    assert tracker.contact_count(PERSONA.customer_id) == 0
