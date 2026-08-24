"""Isolation tests for the contact-cost / annoyance mechanism. Schema §5, §7 step 5."""

from datetime import datetime, timedelta

import pytest

from simulator import (
    Action,
    ActionType,
    Channel,
    ContactTracker,
    Customer,
    DeclineReason,
    InstrumentType,
    Language,
    Payment,
    contact_cost_paise,
    is_customer_facing,
    success_probability,
)
from simulator.config import LIMIT_EXCEEDED_COOLDOWN_HOURS

FAILED_AT = datetime(2026, 1, 1)
ANNOYANCE_THRESHOLD = 3

PERSONA = Customer(
    customer_id="cust_fixed",
    recovery_propensity=0.6,
    funds_arrival_day=2,
    contact_hours=(9, 21),
    annoyance_threshold=ANNOYANCE_THRESHOLD,
    channel_response_rate={"upi_link": 0.7, "whatsapp": 0.6, "sms": 0.5},
    preferred_language=Language.EN,
)


def _payment(decline_reason: DeclineReason, instrument_type=InstrumentType.CARD) -> Payment:
    return Payment(
        payment_id="pay_x",
        customer_id=PERSONA.customer_id,
        amount_paise=100_000,
        instrument_type=instrument_type,
        decline_reason=decline_reason,
        issuer_code="HDFC",
        failed_at=FAILED_AT,
    )


# (decline_reason, action, action_time, instrument_type) chosen so the
# category's formula gives a nonzero baseline probability (not gated on
# contact_count) — otherwise we couldn't tell "gated to 0" from "just 0".
CATEGORY_CASES = [
    (
        DeclineReason.INSUFFICIENT_FUNDS,
        Action(ActionType.RETRY_NOW),
        FAILED_AT + timedelta(days=3),
        InstrumentType.CARD,
    ),
    (
        DeclineReason.MANDATE_INSUFFICIENT_FUNDS,
        Action(ActionType.RETRY_NOW),
        FAILED_AT + timedelta(days=3),
        InstrumentType.MANDATE,
    ),
    (
        DeclineReason.ISSUER_DOWN,
        Action(ActionType.RETRY_NOW),
        FAILED_AT + timedelta(hours=1),
        InstrumentType.CARD,
    ),
    (
        DeclineReason.NETWORK_TIMEOUT,
        Action(ActionType.RETRY_NOW),
        FAILED_AT + timedelta(minutes=1),
        InstrumentType.CARD,
    ),
    (
        DeclineReason.RISK_DECLINE,
        Action(ActionType.ESCALATE_ALTERNATE_INSTRUMENT),
        FAILED_AT,
        InstrumentType.CARD,
    ),
    (
        DeclineReason.CARD_EXPIRED,
        Action(ActionType.SEND_INSTRUMENT_UPDATE_LINK, channel=Channel.SMS),
        FAILED_AT,
        InstrumentType.CARD,
    ),
    (
        DeclineReason.CARD_OR_ACCOUNT_BLOCKED,
        Action(ActionType.SEND_INSTRUMENT_UPDATE_LINK, channel=Channel.SMS),
        FAILED_AT,
        InstrumentType.CARD,
    ),
    (
        DeclineReason.MANDATE_REVOKED,
        Action(ActionType.SEND_INSTRUMENT_UPDATE_LINK, channel=Channel.SMS),
        FAILED_AT,
        InstrumentType.MANDATE,
    ),
    (
        DeclineReason.AFA_3DS_DROPOFF,
        Action(ActionType.SEND_NUDGE, channel=Channel.SMS),
        FAILED_AT + timedelta(minutes=10),
        InstrumentType.CARD,
    ),
    (
        DeclineReason.LIMIT_EXCEEDED,
        Action(ActionType.RETRY_NOW),
        FAILED_AT + timedelta(hours=LIMIT_EXCEEDED_COOLDOWN_HOURS + 1),
        InstrumentType.CARD,
    ),
]


def test_all_ten_categories_covered():
    assert {c[0] for c in CATEGORY_CASES} == set(DeclineReason)


@pytest.mark.parametrize("decline_reason,action,action_time,instrument_type", CATEGORY_CASES)
def test_baseline_is_nonzero_before_opt_out(decline_reason, action, action_time, instrument_type):
    payment = _payment(decline_reason, instrument_type)
    prob = success_probability(payment, PERSONA, action, action_time, contact_count=0)
    assert prob > 0.0, f"{decline_reason} baseline should be nonzero for this setup"


@pytest.mark.parametrize("decline_reason,action,action_time,instrument_type", CATEGORY_CASES)
def test_forced_to_zero_once_over_annoyance_threshold(
    decline_reason, action, action_time, instrument_type
):
    payment = _payment(decline_reason, instrument_type)
    baseline = success_probability(payment, PERSONA, action, action_time, contact_count=0)
    at_threshold = success_probability(
        payment, PERSONA, action, action_time, contact_count=ANNOYANCE_THRESHOLD
    )
    over_threshold = success_probability(
        payment, PERSONA, action, action_time, contact_count=ANNOYANCE_THRESHOLD + 1
    )
    assert at_threshold == baseline, "contact_count == threshold must not be gated yet"
    assert over_threshold == 0.0, f"{decline_reason} must be forced to 0 once over threshold"


def test_tracker_increments_only_on_customer_facing_actions():
    tracker = ContactTracker()
    customer_id = "cust_a"

    silent_actions = [Action(ActionType.RETRY_NOW), Action(ActionType.RETRY_SCHEDULED), Action(ActionType.STOP)]
    for a in silent_actions:
        assert not is_customer_facing(a)
        tracker.record(customer_id, a)
    assert tracker.contact_count(customer_id) == 0
    assert contact_cost_paise(silent_actions[0]) == 0

    customer_facing_action = Action(ActionType.SEND_NUDGE, channel=Channel.SMS)
    assert is_customer_facing(customer_facing_action)
    tracker.record(customer_id, customer_facing_action)
    assert tracker.contact_count(customer_id) == 1
    assert contact_cost_paise(customer_facing_action) > 0


def test_tracker_flips_opted_out_exactly_when_count_exceeds_threshold():
    tracker = ContactTracker()
    customer_id = "cust_b"
    threshold = 3
    action = Action(ActionType.SEND_NUDGE, channel=Channel.SMS)

    # Simulate repeated customer-facing contact. Before each action, check
    # this customer's current state using the count already accumulated.
    for expected_count_before in range(0, threshold + 2):
        count_before = tracker.contact_count(customer_id)
        assert count_before == expected_count_before
        assert tracker.is_opted_out(customer_id, threshold) == (count_before > threshold)
        tracker.record(customer_id, action)

    # After threshold+2 recorded contacts, contact_count is threshold+2 > threshold.
    assert tracker.contact_count(customer_id) == threshold + 2
    assert tracker.is_opted_out(customer_id, threshold) is True


def test_customer_never_recovers_after_opt_out_even_with_favorable_timing():
    """Once opted out, no later action_time or action makes them recoverable again —
    "permanently unrecoverable regardless of what happens afterward" (§5)."""
    payment = _payment(DeclineReason.INSUFFICIENT_FUNDS, InstrumentType.CARD)
    over_threshold_count = ANNOYANCE_THRESHOLD + 1

    far_future_after_funds_arrive = FAILED_AT + timedelta(days=20)
    prob = success_probability(
        payment,
        PERSONA,
        Action(ActionType.SEND_PAYMENT_LINK, channel=Channel.UPI_LINK),
        far_future_after_funds_arrive,
        contact_count=over_threshold_count,
    )
    assert prob == 0.0
