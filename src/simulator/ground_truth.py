"""Ground-truth success function. Schema §4 (formulas) + §5 (annoyance gate).

    success_probability(payment, customer, action, action_time,
                         contact_count, outage_events=None) -> float [0, 1]

Pure: no I/O, no randomness, no mutation. All state (contact_count, outage
windows) comes in as arguments rather than being read off mutable objects, so
the same inputs always give the same output — the harness is what samples the
Bernoulli draw from this, this function only ever returns a probability.

`contact_count` is the number of customer-facing contacts already made to this
customer before `action`. Per §5, the annoyance/opt-out check must live inside
this function, not be special-cased by the harness, so it's the first thing
checked below.

`outage_events` distinguishes ISSUER_DOWN payments caused by the true_outage
from the decoy_cluster: only a true_outage window forces probability to 0;
per §3, the decoy gets "normal idiosyncratic decay" like any other
ISSUER_DOWN case with no outage at all.
"""

import math
from datetime import datetime, timedelta
from typing import List, Optional

from . import config
from .types import Action, ActionType, Customer, DeclineReason, OutageEvent, Payment

_RETRY_ACTIONS = frozenset({ActionType.RETRY_NOW, ActionType.RETRY_SCHEDULED})


def _hours_between(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 3600.0


def _find_true_outage(
    payment: Payment, outage_events: List[OutageEvent]
) -> Optional[OutageEvent]:
    for event in outage_events:
        if event.kind == "true_outage" and event.issuer_code == payment.issuer_code:
            return event
    return None


def _insufficient_funds(payment: Payment, customer: Customer, action_time: datetime) -> float:
    funds_arrival_time = payment.failed_at + timedelta(days=customer.funds_arrival_day)
    if action_time < funds_arrival_time:
        return 0.0
    days_past_arrival = _hours_between(action_time, funds_arrival_time) / 24.0
    return customer.recovery_propensity * math.exp(
        -config.K1_FUNDS_ARRIVAL_DECAY * days_past_arrival
    )


def _issuer_down(
    payment: Payment,
    customer: Customer,
    action_time: datetime,
    outage_events: List[OutageEvent],
) -> float:
    outage = _find_true_outage(payment, outage_events)
    if outage is not None:
        outage_end = outage.start_time + timedelta(minutes=outage.duration_minutes)
        if outage.start_time <= action_time <= outage_end:
            return 0.0
        if action_time > outage_end:
            hours_since_recovery = _hours_between(action_time, outage_end)
        else:
            # action_time is before this issuer's outage even starts.
            hours_since_recovery = _hours_between(action_time, payment.failed_at)
    else:
        # No true outage for this issuer (e.g. a decoy_cluster payment) —
        # normal idiosyncratic decay from time of failure, per schema §3.
        hours_since_recovery = _hours_between(action_time, payment.failed_at)
    return customer.recovery_propensity * math.exp(
        -config.K2_ISSUER_RECOVERY_DECAY * hours_since_recovery
    )


def _network_timeout(payment: Payment, customer: Customer, action_time: datetime) -> float:
    minutes_since_failure = _hours_between(action_time, payment.failed_at) * 60.0
    return customer.recovery_propensity * math.exp(
        -config.K3_NETWORK_TIMEOUT_DECAY * minutes_since_failure
    )


def _risk_decline(customer: Customer, action: Action) -> float:
    if action.action_type == ActionType.ESCALATE_ALTERNATE_INSTRUMENT:
        return customer.recovery_propensity * config.RISK_DECLINE_ESCALATE_MULTIPLIER
    return 0.0


def _terminal(customer: Customer, action: Action) -> float:
    if action.action_type == ActionType.SEND_INSTRUMENT_UPDATE_LINK and action.channel:
        return customer.recovery_propensity * customer.channel_response_rate[action.channel.value]
    return 0.0


def _afa_3ds_dropoff(
    payment: Payment, customer: Customer, action: Action, action_time: datetime
) -> float:
    if action.action_type in _RETRY_ACTIONS or not action.channel:
        return 0.0
    hours_since_failure = _hours_between(action_time, payment.failed_at)
    return (
        customer.recovery_propensity
        * customer.channel_response_rate[action.channel.value]
        * math.exp(-config.K4_AFA_DROPOFF_DECAY * hours_since_failure)
    )


def _limit_exceeded(payment: Payment, customer: Customer, action_time: datetime) -> float:
    cooldown_end = payment.failed_at + timedelta(hours=config.LIMIT_EXCEEDED_COOLDOWN_HOURS)
    if action_time < cooldown_end:
        return 0.0
    hours_since_cooldown_start = _hours_between(action_time, payment.failed_at)
    return customer.recovery_propensity * math.exp(
        -config.K5_LIMIT_EXCEEDED_DECAY * hours_since_cooldown_start
    )


def success_probability(
    payment: Payment,
    customer: Customer,
    action: Action,
    action_time: datetime,
    contact_count: int,
    outage_events: Optional[List[OutageEvent]] = None,
) -> float:
    """Ground-truth probability that `action` at `action_time` recovers `payment`."""
    if contact_count > customer.annoyance_threshold:
        return 0.0

    outage_events = outage_events or []
    category = payment.decline_reason

    if category in (DeclineReason.INSUFFICIENT_FUNDS, DeclineReason.MANDATE_INSUFFICIENT_FUNDS):
        probability = _insufficient_funds(payment, customer, action_time)
    elif category == DeclineReason.ISSUER_DOWN:
        probability = _issuer_down(payment, customer, action_time, outage_events)
    elif category == DeclineReason.NETWORK_TIMEOUT:
        probability = _network_timeout(payment, customer, action_time)
    elif category == DeclineReason.RISK_DECLINE:
        probability = _risk_decline(customer, action)
    elif category in (
        DeclineReason.CARD_EXPIRED,
        DeclineReason.CARD_OR_ACCOUNT_BLOCKED,
        DeclineReason.MANDATE_REVOKED,
    ):
        probability = _terminal(customer, action)
    elif category == DeclineReason.AFA_3DS_DROPOFF:
        probability = _afa_3ds_dropoff(payment, customer, action, action_time)
    elif category == DeclineReason.LIMIT_EXCEEDED:
        probability = _limit_exceeded(payment, customer, action_time)
    else:  # pragma: no cover - exhaustive over DeclineReason
        raise ValueError(f"Unhandled decline_reason: {category}")

    return min(1.0, max(0.0, probability))
