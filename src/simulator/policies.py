"""Baseline policies. Eval spec §3, schema §7 step 6.

Each policy is a pure function:

    policy_fn(payment, customer) -> List[Tuple[timedelta, Action]]

...a plan of (offset-from-failure, Action) pairs. Policies do NOT see any
hidden ground truth (recovery_propensity, funds_arrival_day,
annoyance_threshold, ...) — only the fields a real system would actually
observe: payment.decline_reason, payment.instrument_type, etc. The harness
executes the plan against ground truth and stops early on success.

full_agent is an intentional NotImplementedError stub this round — not the
real decision policy yet.
"""

from datetime import timedelta
from typing import List, Tuple

from .types import Action, ActionType, Channel, Customer, DeclineReason, Payment

Plan = List[Tuple[timedelta, Action]]


def do_nothing_policy(payment: Payment, customer: Customer) -> Plan:
    """Floor baseline: takes no action, ever."""
    return []


# ASSUMPTION: exact retry cadence. Eval spec §3 gives "3 attempts, 1 hour
# apart, then give up" — that's followed verbatim; nothing else is unspecified
# here.
NAIVE_RETRY_OFFSETS_HOURS = (1, 2, 3)


def naive_fixed_retry_policy(payment: Payment, customer: Customer) -> Plan:
    """Industry default: 3 retries, 1hr apart, no taxonomy awareness.

    Each attempt is explicitly customer-facing (a notification goes out with
    it) — see DECISIONS.md 2026-08-25: this is what lets contact_count (and
    therefore Rs/contact) differentiate naive from a taxonomy-aware policy.
    """
    return [
        (
            timedelta(hours=h),
            Action(ActionType.RETRY_NOW, customer_facing=True),
        )
        for h in NAIVE_RETRY_OFFSETS_HOURS
    ]


# ASSUMPTION: rules_only's per-category routing table. The eval spec §1
# taxonomy gives qualitative guidance ("short retry", "few hours", ...) per
# category, not exact action_types/delays/channels — those are fixed here,
# one lookup per category, no retry campaigns and no systemic detection
# (a real ISSUER_DOWN response would detect the spike and wait for recovery;
# rules_only just applies the category's generic delay, per eval spec §3:
# "no systemic detection ... deterministic routing only").
#
# MANDATE_REVOKED routes to STOP, not send_instrument_update_link, even
# though ground_truth.py's terminal-category formula would technically credit
# a nonzero probability for that action: the eval spec's taxonomy explicitly
# calls MANDATE_REVOKED "outside auto-retry scope" (needs fresh mandate
# consent), so a rules-based policy correctly declines to auto-act on it.
RULES_ONLY_TABLE = {
    DeclineReason.INSUFFICIENT_FUNDS: (
        timedelta(hours=1),
        ActionType.SEND_PAYMENT_LINK,
        Channel.UPI_LINK,
    ),
    DeclineReason.MANDATE_INSUFFICIENT_FUNDS: (
        timedelta(hours=1),
        ActionType.SEND_PAYMENT_LINK,
        Channel.UPI_LINK,
    ),
    DeclineReason.ISSUER_DOWN: (
        timedelta(minutes=20),
        ActionType.RETRY_SCHEDULED,
        None,
    ),
    DeclineReason.NETWORK_TIMEOUT: (
        timedelta(minutes=2),
        ActionType.RETRY_NOW,
        None,
    ),
    DeclineReason.RISK_DECLINE: (
        timedelta(minutes=0),
        ActionType.ESCALATE_ALTERNATE_INSTRUMENT,
        None,
    ),
    DeclineReason.CARD_EXPIRED: (
        timedelta(minutes=0),
        ActionType.SEND_INSTRUMENT_UPDATE_LINK,
        Channel.SMS,
    ),
    DeclineReason.CARD_OR_ACCOUNT_BLOCKED: (
        timedelta(minutes=0),
        ActionType.SEND_INSTRUMENT_UPDATE_LINK,
        Channel.SMS,
    ),
    DeclineReason.MANDATE_REVOKED: (
        timedelta(minutes=0),
        ActionType.STOP,
        None,
    ),
    DeclineReason.AFA_3DS_DROPOFF: (
        timedelta(minutes=30),
        ActionType.SEND_NUDGE,
        Channel.WHATSAPP,
    ),
    DeclineReason.LIMIT_EXCEEDED: (
        timedelta(hours=5),
        ActionType.RETRY_SCHEDULED,
        None,
    ),
}


def rules_only_policy(payment: Payment, customer: Customer) -> Plan:
    """Taxonomy-category lookup drives the action. No systemic detection, no
    LLM, no cost scoring — deterministic routing only, per eval spec §3."""
    offset, action_type, channel = RULES_ONLY_TABLE[payment.decline_reason]
    if action_type == ActionType.STOP:
        return []
    return [(offset, Action(action_type, channel=channel))]


# full_agent lives in full_agent.py, not here: it needs batch-level context
# (the failure log for the systemic detector, a shared ContactTracker for
# compliance vetoes) that the plain (payment, customer) -> Plan signature
# above can't carry. See harness.run_eval for how it's constructed and run
# alongside these three. Deliberately not in POLICIES below — it isn't
# constructable from a payment/customer pair alone.
POLICIES = {
    "do_nothing": do_nothing_policy,
    "naive_fixed_retry": naive_fixed_retry_policy,
    "rules_only": rules_only_policy,
}
