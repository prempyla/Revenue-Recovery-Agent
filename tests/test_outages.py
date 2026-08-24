"""Isolation tests for outage + decoy injection. Schema §3, §7 step 4."""

from datetime import datetime, timedelta

from simulator import (
    Action,
    ActionType,
    Customer,
    DeclineReason,
    InstrumentType,
    Language,
    Payment,
    generate_customers,
    generate_outage_events,
    inject_outage_events,
    success_probability,
)

WINDOW_START = datetime(2026, 1, 1)
WINDOW_DAYS = 30

PERSONA = Customer(
    customer_id="cust_fixed",
    recovery_propensity=0.6,
    funds_arrival_day=5,
    contact_hours=(9, 21),
    annoyance_threshold=10,
    channel_response_rate={"upi_link": 0.7, "whatsapp": 0.6, "sms": 0.5},
    preferred_language=Language.EN,
)


def _events():
    return generate_outage_events(
        window_start=WINDOW_START, window_days=WINDOW_DAYS, seed=1
    )


def test_generates_one_true_outage_and_one_decoy_on_different_issuers():
    events = _events()
    kinds = sorted(e.kind for e in events)
    assert kinds == ["decoy_cluster", "true_outage"]
    assert events[0].issuer_code != events[1].issuer_code
    for e in events:
        # mid-window: not day 1, not day 30
        offset_days = (e.start_time - WINDOW_START).total_seconds() / 86400
        assert 1 < offset_days < WINDOW_DAYS - 1


def test_true_outage_forces_zero_regardless_of_action_during_window():
    events = _events()
    true_outage = next(e for e in events if e.kind == "true_outage")
    payment = Payment(
        payment_id="pay_x",
        customer_id=PERSONA.customer_id,
        amount_paise=100_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=DeclineReason.ISSUER_DOWN,
        issuer_code=true_outage.issuer_code,
        failed_at=true_outage.start_time,
    )
    mid_window_time = true_outage.start_time + timedelta(
        minutes=true_outage.duration_minutes / 2
    )
    for action in [
        Action(ActionType.RETRY_NOW),
        Action(ActionType.RETRY_SCHEDULED),
        Action(ActionType.SEND_NUDGE, channel=None),
        Action(ActionType.STOP),
    ]:
        prob = success_probability(
            payment, PERSONA, action, mid_window_time, contact_count=0, outage_events=events
        )
        assert prob == 0.0, f"expected 0 during outage window for {action.action_type}"


def test_normal_decay_resumes_shortly_after_true_outage():
    events = _events()
    true_outage = next(e for e in events if e.kind == "true_outage")
    payment = Payment(
        payment_id="pay_x",
        customer_id=PERSONA.customer_id,
        amount_paise=100_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=DeclineReason.ISSUER_DOWN,
        issuer_code=true_outage.issuer_code,
        failed_at=true_outage.start_time,
    )
    outage_end = true_outage.start_time + timedelta(minutes=true_outage.duration_minutes)

    just_before_end = success_probability(
        payment, PERSONA, Action(ActionType.RETRY_NOW), outage_end - timedelta(seconds=1),
        contact_count=0, outage_events=events,
    )
    just_after_end = success_probability(
        payment, PERSONA, Action(ActionType.RETRY_NOW), outage_end + timedelta(minutes=1),
        contact_count=0, outage_events=events,
    )
    assert just_before_end == 0.0
    assert just_after_end > 0.5  # jumps back near recovery_propensity=0.6, barely decayed


def test_decoy_cluster_gets_no_special_case_handling():
    """A decoy_cluster payment must decay identically to an ISSUER_DOWN payment
    with no outage_events at all — i.e. its own outage entry (kind=decoy_cluster)
    has zero effect on success_probability, per §3."""
    events = _events()
    decoy = next(e for e in events if e.kind == "decoy_cluster")
    payment = Payment(
        payment_id="pay_decoy",
        customer_id=PERSONA.customer_id,
        amount_paise=100_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=DeclineReason.ISSUER_DOWN,
        issuer_code=decoy.issuer_code,
        failed_at=decoy.start_time,
    )
    action = Action(ActionType.RETRY_NOW)

    for minutes_offset in [0, 5, 30, 120]:
        action_time = decoy.start_time + timedelta(minutes=minutes_offset)
        with_decoy = success_probability(
            payment, PERSONA, action, action_time, contact_count=0, outage_events=events
        )
        no_outage_at_all = success_probability(
            payment, PERSONA, action, action_time, contact_count=0, outage_events=[]
        )
        assert with_decoy == no_outage_at_all, (
            f"decoy_cluster must not alter success_probability at +{minutes_offset}min"
        )
        # and specifically: decoy does NOT force 0 during its own window,
        # unlike a true_outage would.
        if minutes_offset < decoy.duration_minutes:
            assert with_decoy > 0.0


def test_injection_forces_existing_in_window_payment_to_issuer_down():
    events = _events()
    true_outage = next(e for e in events if e.kind == "true_outage")
    customers = generate_customers(10, seed=2)
    background_payment = Payment(
        payment_id="pay_bg",
        customer_id=customers[0].customer_id,
        amount_paise=100_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=DeclineReason.NETWORK_TIMEOUT,  # deliberately wrong reason
        issuer_code=true_outage.issuer_code,
        failed_at=true_outage.start_time + timedelta(minutes=5),
    )
    injected = inject_outage_events([background_payment], customers, events, seed=3)
    forced = next(p for p in injected if p.payment_id == "pay_bg")
    assert forced.decline_reason == DeclineReason.ISSUER_DOWN


def test_injection_adds_cluster_burst_within_each_window():
    events = _events()
    customers = generate_customers(50, seed=4)
    injected = inject_outage_events([], customers, events, seed=5)

    for event in events:
        window_end = event.start_time + timedelta(minutes=event.duration_minutes)
        cluster = [
            p
            for p in injected
            if p.issuer_code == event.issuer_code
            and event.start_time <= p.failed_at <= window_end
            and p.decline_reason == DeclineReason.ISSUER_DOWN
        ]
        expected_min_size = 4 if event.kind == "decoy_cluster" else 10
        assert len(cluster) >= expected_min_size, (
            f"{event.kind} cluster too small: {len(cluster)}"
        )

    true_size = sum(1 for p in injected if "true_outage" in p.payment_id)
    decoy_size = sum(1 for p in injected if "decoy_cluster" in p.payment_id)
    assert true_size > decoy_size  # true outage cluster is bigger, decoy is "smaller"
