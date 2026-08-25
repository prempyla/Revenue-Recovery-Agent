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

SIMULATED_SENT (2026-08-25) is the same discipline applied a third time.
Before this state existed, a simulated action (outbox.py's
SIMULATED_ACTION_TYPES — no real Razorpay primitive behind it) transitioned
to AWAITING_CONFIRMATION exactly like a real send, for FSM-shape
consistency. But nothing can ever confirm it: no webhook will arrive for a
send that never happened, and reconciliation.py's poller would eventually
find it "stuck", query Razorpay, find no record (correctly — nothing was
ever sent), and mark it ABANDONED(execution_error) — writing a FALSE
statement into an append-only ledger: "the system tried and failed" when
the truth is "the system worked exactly as designed and there was never
anything to confirm." Same class of problem as the two entries above (see
DECISIONS.md 2026-08-25's "recorded reason must be true" entry for the
general rule this is the third instance of) — right behavior, wrong
recorded reason. SIMULATED_SENT is reached directly from EXECUTING and is
terminal; reconciliation.py's STALE_STATES tuple deliberately excludes it,
so the poller never even considers a simulated action "stuck" in the first
place. Legal only for actions the outbox worker itself tags simulated=True
in the result payload — enforced at the call site (outbox.py), not by the
FSM itself, same as AbandonReason isn't validated against which specific
scenario produced it.
"""

from enum import Enum
from typing import Optional


class PaymentState(str, Enum):
    AT_RISK = "at_risk"
    DIAGNOSED = "diagnosed"
    SCHEDULED = "scheduled"
    EXECUTING = "executing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    SIMULATED_SENT = "simulated_sent"
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
    PaymentState.EXECUTING: {
        PaymentState.AWAITING_CONFIRMATION,
        PaymentState.SIMULATED_SENT,
        PaymentState.ABANDONED,
    },
    PaymentState.AWAITING_CONFIRMATION: {PaymentState.RECOVERED, PaymentState.ABANDONED},
    PaymentState.SIMULATED_SENT: set(),  # terminal -- nothing can ever confirm a simulated send
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
