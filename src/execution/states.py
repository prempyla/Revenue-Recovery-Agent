"""Payment execution state machine.

Explicit FSM, declared legal transitions only — no boolean flags scattered
across records. validate_transition() is the single place that decides
whether a transition is legal; both eventlog.append_event() (write path) and
eventlog.derive_state() (replay/audit path) go through it, so a corrupted
event sequence is caught on replay, not just trusted.

ABANDONED is one state but three distinct situations can lead to it — the
policy decided not to act, the execution call itself failed, or the customer
never completed payment. Collapsing those into a single state without
recording which one happened would make the audit trail useless for
answering "why did we give up on this payment" after the fact. Every
transition INTO abandoned must carry an AbandonReason.
"""

from enum import Enum
from typing import Optional


class PaymentState(str, Enum):
    AT_RISK = "at_risk"
    DIAGNOSED = "diagnosed"
    SCHEDULED = "scheduled"
    EXECUTING = "executing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    RECOVERED = "recovered"
    ABANDONED = "abandoned"


class AbandonReason(str, Enum):
    POLICY_STOP = "policy_stop"  # the policy looked at it and chose not to act
    EXECUTION_ERROR = "execution_error"  # the API call itself raised/failed
    PAYMENT_FAILED = "payment_failed"  # webhook confirmed the customer didn't pay


ALLOWED_TRANSITIONS = {
    None: {PaymentState.AT_RISK},  # the very first event for a payment
    PaymentState.AT_RISK: {PaymentState.DIAGNOSED},
    PaymentState.DIAGNOSED: {PaymentState.SCHEDULED, PaymentState.ABANDONED},
    PaymentState.SCHEDULED: {PaymentState.EXECUTING, PaymentState.ABANDONED},
    PaymentState.EXECUTING: {PaymentState.AWAITING_CONFIRMATION, PaymentState.ABANDONED},
    PaymentState.AWAITING_CONFIRMATION: {PaymentState.RECOVERED, PaymentState.ABANDONED},
    PaymentState.RECOVERED: set(),
    PaymentState.ABANDONED: set(),
}


class IllegalStateTransition(Exception):
    pass


def validate_transition(
    from_state: Optional[PaymentState],
    to_state: PaymentState,
    abandon_reason: Optional[AbandonReason] = None,
) -> None:
    """Raises IllegalStateTransition if from_state -> to_state isn't a legal
    edge, or if transitioning to ABANDONED without an AbandonReason."""
    allowed = ALLOWED_TRANSITIONS.get(from_state, set())
    if to_state not in allowed:
        raise IllegalStateTransition(f"{from_state} -> {to_state} is not a legal transition")
    if to_state == PaymentState.ABANDONED and abandon_reason is None:
        raise IllegalStateTransition("transition to ABANDONED requires an AbandonReason")
    if to_state != PaymentState.ABANDONED and abandon_reason is not None:
        raise IllegalStateTransition("abandon_reason is only valid on a transition to ABANDONED")
