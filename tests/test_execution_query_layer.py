"""query_layer.py: recent_failures() and contact_tracker_for() derive the
shapes full_agent.decide() expects from execution/'s own event log.
Injected clock throughout, FakeRazorpayClient only, no network.

Two assertions the task specifically asked for:
  1. seeded failure events across issuers produce a failure log that makes
     detect_systemic_event fire exactly as it does on the ORIGINAL
     simulator Payment objects for the same scenario.
  2. a customer with prior contact events produces a tracker whose vetoes
     fire correctly (weekly_cap, via full_agent.decide()).
"""

from datetime import datetime, timedelta

from simulator import config
from simulator.full_agent import decide
from simulator.outage_detector import OutageDetectorConfig, detect_systemic_event
from simulator.policies import rules_only_policy
from simulator.types import Action, ActionType, Customer, DeclineReason, InstrumentType, Language, Payment

from execution.db import make_engine, make_session_factory
from execution.orchestrator import diagnose_and_schedule
from execution.outbox import run_outbox_worker_once
from execution.query_layer import contact_tracker_for, recent_failures
from execution.razorpay_client import FakeRazorpayClient

WINDOW_START = datetime(2026, 1, 1, 10, 0)

PERSONA = Customer(
    customer_id="cust_query_layer",
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


# --- recent_failures() vs. the systemic detector ---


def _issuer_down_payment(payment_id, issuer_code, failed_at, customer_id=PERSONA.customer_id):
    return Payment(
        payment_id=payment_id,
        customer_id=customer_id,
        amount_paise=100_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=DeclineReason.ISSUER_DOWN,
        issuer_code=issuer_code,
        failed_at=failed_at,
    )


def test_recent_failures_makes_the_detector_fire_identically_to_simulator_data():
    """A dense burst on one issuer -- diagnosed through the real execution
    path -- must make detect_systemic_event fire at the exact same instants
    it would on the original in-memory Payment list."""
    detector_cfg = OutageDetectorConfig(window_minutes=15, count_threshold=3, cooldown_minutes=15)
    burst = [
        _issuer_down_payment(f"pay_burst_{i}", "HDFC", WINDOW_START + timedelta(minutes=m))
        for i, m in enumerate([0, 3, 6, 9, 12])
    ]

    session = _session()
    for payment in burst:
        diagnose_and_schedule(session, payment, PERSONA, payment.failed_at, policy_fn=rules_only_policy)

    reconstructed = recent_failures(session, now=WINDOW_START + timedelta(hours=1), lookback=timedelta(hours=6))
    assert len(reconstructed) == len(burst)

    # Compare detect_systemic_event's verdict at several instants, original
    # simulator data vs. the execution-log-reconstructed failure log.
    for probe_minutes in (0, 5, 10, 12, 15, 20, 30, 45, 60):
        probe_time = WINDOW_START + timedelta(minutes=probe_minutes)
        original_verdict = detect_systemic_event(burst, "HDFC", probe_time, detector_cfg)
        reconstructed_verdict = detect_systemic_event(reconstructed, "HDFC", probe_time, detector_cfg)
        assert reconstructed_verdict == original_verdict, probe_minutes

    # And it genuinely does fire True somewhere in there -- not a vacuous
    # comparison of two always-False lists.
    assert any(
        detect_systemic_event(burst, "HDFC", WINDOW_START + timedelta(minutes=m), detector_cfg)
        for m in range(0, 60)
    )


def test_recent_failures_matches_simulator_on_a_sparse_non_triggering_scenario_too():
    """The negative case: too sparse / wrong issuer to ever trigger --
    confirms the comparison isn't just agreeing because both sides are
    trivially True."""
    detector_cfg = OutageDetectorConfig(window_minutes=15, count_threshold=3, cooldown_minutes=15)
    sparse = [
        _issuer_down_payment("pay_sparse_1", "HDFC", WINDOW_START),
        _issuer_down_payment("pay_sparse_2", "ICICI", WINDOW_START + timedelta(minutes=5)),
        _issuer_down_payment("pay_sparse_3", "HDFC", WINDOW_START + timedelta(hours=2)),
    ]

    session = _session()
    for payment in sparse:
        diagnose_and_schedule(session, payment, PERSONA, payment.failed_at, policy_fn=rules_only_policy)

    reconstructed = recent_failures(session, now=WINDOW_START + timedelta(hours=3), lookback=timedelta(hours=6))

    for issuer in ("HDFC", "ICICI"):
        for probe_minutes in (0, 30, 60, 90, 120, 150):
            probe_time = WINDOW_START + timedelta(minutes=probe_minutes)
            original_verdict = detect_systemic_event(sparse, issuer, probe_time, detector_cfg)
            reconstructed_verdict = detect_systemic_event(reconstructed, issuer, probe_time, detector_cfg)
            assert reconstructed_verdict == original_verdict is False, (issuer, probe_minutes)


def test_recent_failures_respects_the_lookback_window():
    session = _session()
    old_payment = _issuer_down_payment("pay_old", "HDFC", WINDOW_START)
    recent_payment = _issuer_down_payment("pay_recent", "HDFC", WINDOW_START + timedelta(hours=5))
    diagnose_and_schedule(session, old_payment, PERSONA, old_payment.failed_at, policy_fn=rules_only_policy)
    diagnose_and_schedule(
        session, recent_payment, PERSONA, recent_payment.failed_at, policy_fn=rules_only_policy
    )

    now = WINDOW_START + timedelta(hours=6)
    result = recent_failures(session, now=now, lookback=timedelta(hours=2))
    payment_ids = {p.payment_id for p in result}
    assert "pay_recent" in payment_ids
    assert "pay_old" not in payment_ids


def test_recent_failures_skips_events_missing_required_fields_rather_than_fabricating():
    """Simulates a pre-fix / corrupted DIAGNOSED event (missing issuer_code
    etc.) -- must be skipped, never given a fabricated value."""
    from execution.eventlog import append_event
    from execution.states import PaymentState

    session = _session()
    append_event(session, "pay_incomplete", PaymentState.AT_RISK, WINDOW_START)
    append_event(
        session, "pay_incomplete", PaymentState.DIAGNOSED, WINDOW_START,
        payload={"decline_reason": "ISSUER_DOWN"},  # missing customer_id/amount_paise/instrument_type/issuer_code
    )
    session.commit()

    result = recent_failures(session, now=WINDOW_START + timedelta(hours=1), lookback=timedelta(hours=6))
    assert result == []


# --- contact_tracker_for() vs. full_agent's compliance vetoes ---


def _customer_facing_payment(payment_id, failed_at):
    return Payment(
        payment_id=payment_id,
        customer_id=PERSONA.customer_id,
        amount_paise=500_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=DeclineReason.AFA_3DS_DROPOFF,  # full_agent routes this to send_nudge, customer-facing
        issuer_code="HDFC",
        failed_at=failed_at,
    )


def _diagnose_schedule_and_dispatch(session, payment, client, policy_fn):
    intent = diagnose_and_schedule(session, payment, PERSONA, payment.failed_at, policy_fn=policy_fn)
    assert intent is not None
    run_outbox_worker_once(session, client, intent.due_at)
    return intent


def test_contact_tracker_for_reconstructs_the_correct_count():
    from simulator.full_agent import make_full_agent_policy
    from simulator import ContactTracker as FreshTracker

    session = _session()
    client = FakeRazorpayClient()
    policy_fn = make_full_agent_policy(failure_log=[], outage_events=[])(FreshTracker())

    for i in range(2):
        payment = _customer_facing_payment(f"pay_contact_{i}", WINDOW_START + timedelta(hours=i))
        _diagnose_schedule_and_dispatch(session, payment, client, policy_fn)

    tracker = contact_tracker_for(session, PERSONA.customer_id)
    assert tracker.contact_count(PERSONA.customer_id) == 2


def test_contact_tracker_for_produces_a_tracker_whose_weekly_cap_veto_fires_correctly():
    """The actual requirement: feed the reconstructed tracker into
    full_agent.decide() and confirm the SAME weekly_cap veto that already
    works in-memory (test_full_agent.py) also fires when the tracker comes
    from the real execution log instead."""
    from simulator.full_agent import make_full_agent_policy
    from simulator import ContactTracker as FreshTracker

    session = _session()
    client = FakeRazorpayClient()
    policy_fn = make_full_agent_policy(failure_log=[], outage_events=[])(FreshTracker())

    # Exactly MAX_WEEKLY_CONTACTS customer-facing dispatches -- one more
    # than this should trip the veto (config.MAX_WEEKLY_CONTACTS=3: veto
    # fires once contact_count >= 3).
    for i in range(config.MAX_WEEKLY_CONTACTS):
        payment = _customer_facing_payment(f"pay_cap_{i}", WINDOW_START + timedelta(hours=i))
        _diagnose_schedule_and_dispatch(session, payment, client, policy_fn)

    tracker = contact_tracker_for(session, PERSONA.customer_id)
    assert tracker.contact_count(PERSONA.customer_id) == config.MAX_WEEKLY_CONTACTS

    new_payment = _customer_facing_payment("pay_should_be_vetoed", WINDOW_START + timedelta(hours=10))
    decision = decide(new_payment, PERSONA, new_payment.failed_at, tracker, failure_log=[], outage_events=[])
    assert decision is None  # vetoed by weekly_cap


def test_contact_tracker_for_does_not_veto_when_under_the_cap():
    from simulator.full_agent import make_full_agent_policy
    from simulator import ContactTracker as FreshTracker

    session = _session()
    client = FakeRazorpayClient()
    policy_fn = make_full_agent_policy(failure_log=[], outage_events=[])(FreshTracker())

    for i in range(config.MAX_WEEKLY_CONTACTS - 1):
        payment = _customer_facing_payment(f"pay_under_cap_{i}", WINDOW_START + timedelta(hours=i))
        _diagnose_schedule_and_dispatch(session, payment, client, policy_fn)

    tracker = contact_tracker_for(session, PERSONA.customer_id)
    assert tracker.contact_count(PERSONA.customer_id) == config.MAX_WEEKLY_CONTACTS - 1

    new_payment = _customer_facing_payment("pay_should_not_be_vetoed", WINDOW_START + timedelta(hours=10))
    decision = decide(new_payment, PERSONA, new_payment.failed_at, tracker, failure_log=[], outage_events=[])
    assert decision is not None


def test_contact_tracker_for_does_not_count_scheduled_but_not_yet_dispatched_intents():
    """A payment that's only SCHEDULED (not yet due, worker hasn't run)
    isn't a real contact yet -- must not be counted."""
    session = _session()
    payment = _customer_facing_payment("pay_not_dispatched", WINDOW_START)
    intent = diagnose_and_schedule(session, payment, PERSONA, WINDOW_START, policy_fn=rules_only_policy)
    assert intent is not None
    # Deliberately do NOT run the worker.

    tracker = contact_tracker_for(session, PERSONA.customer_id)
    assert tracker.contact_count(PERSONA.customer_id) == 0


def test_contact_tracker_for_never_reports_opted_out_since_nothing_persists_it():
    """Honest reflection of the reported gap: no opt-out event exists in
    execution/'s log at all, so the reconstructed tracker can never say a
    customer opted out -- not a false negative, just what's actually there."""
    session = _session()
    tracker = contact_tracker_for(session, PERSONA.customer_id)
    assert tracker.is_opted_out(PERSONA.customer_id) is False
