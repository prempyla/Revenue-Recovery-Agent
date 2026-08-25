"""Ties a decision policy + the state machine + the outbox into one path:
policy decides -> action persisted (with due_at) -> (separately) worker
respects due_at and executes.

2026-08-25 audit fix #2/#8: previously hardcoded rules_only_policy and
discarded the policy's decided offset entirely (`_offset, action = plan[0]`)
— every intent fired immediately regardless of what was decided, and
full_agent had never driven this path at all. Now:
  - policy_fn is a parameter (Callable[[Payment, Customer], Plan]),
    defaulting to rules_only_policy so every existing caller that doesn't
    pass one keeps behaving exactly as before.
  - the decided offset becomes due_at = now + offset on the outbox intent
    (outbox.py), no longer thrown away.
  - the previous "abandon anything that isn't send_payment_link" branch is
    GONE. It existed only because the worker couldn't execute anything
    else; now that outbox.py dispatches every action_type either as a real
    Razorpay call (send_payment_link) or a logged simulated send (every
    other type), there's nothing left for that branch to guard against.
    This is a genuine, deliberate behavior change, not a silent one — see
    DECISIONS.md 2026-08-25 for exactly which existing test's premise this
    invalidated and how it was updated, not worked around.

Interface friction, reported rather than hidden: full_agent's decide()
needs failure_log/outage_events/a ContactTracker that a plain
(payment, customer) -> Plan policy_fn doesn't carry. Wiring full_agent in
here means the CALLER must pre-bind those via
make_full_agent_policy(failure_log, outage_events)(tracker) before passing
the result as policy_fn — this module stays agnostic about how policy_fn
was built. There is no persistent store in execution/ yet that would let a
real deployment source failure_log/outage_events for full_agent's systemic
detector from live data; tests construct them explicitly. That gap is
scoped out of this round (see DECISIONS.md).

simulator.types.Customer is a synthetic ground-truth persona with no real
name or phone number (by design — no PII in the simulator). customer_name/
customer_contact below are therefore clearly-synthetic placeholders, not
real contact info. Real notifications are left disabled in
RealRazorpayClient's payload (notify.sms/email = False) — see
razorpay_client.py — so nothing gets sent to a fake number; the returned
short_url is what a human uses for manual verification.
"""

from datetime import datetime
from typing import Callable, Optional

from sqlalchemy.orm import Session

from simulator.policies import Plan, rules_only_policy
from simulator.types import Customer, Payment

from .eventlog import append_event
from .idempotency import make_idempotency_key
from .outbox import OutboxIntent, write_intent_with_state_change
from .states import AbandonReason, PaymentState

SYNTHETIC_CONTACT_PLACEHOLDER = "9000000000"  # not a real number; notify is disabled

PolicyFn = Callable[[Payment, Customer], Plan]


def diagnose_and_schedule(
    session: Session,
    payment: Payment,
    customer: Customer,
    now: datetime,
    policy_fn: PolicyFn = rules_only_policy,
    attempt_number: int = 1,
) -> Optional[OutboxIntent]:
    """AT_RISK -> DIAGNOSED, then either SCHEDULED+outbox-intent (with
    due_at = now + the policy's decided offset) or ABANDONED(policy_stop) if
    the policy returned no action. Returns the OutboxIntent if one was
    scheduled, None if abandoned at diagnosis."""
    append_event(session, payment.payment_id, PaymentState.AT_RISK, now)
    append_event(
        session,
        payment.payment_id,
        PaymentState.DIAGNOSED,
        now,
        payload={"decline_reason": payment.decline_reason.value},
    )
    session.commit()

    plan = policy_fn(payment, customer)

    if not plan:
        policy_name = getattr(policy_fn, "__name__", repr(policy_fn))
        append_event(
            session,
            payment.payment_id,
            PaymentState.ABANDONED,
            now,
            abandon_reason=AbandonReason.POLICY_STOP,
            payload={"reason": f"{policy_name} returned no action for this category"},
        )
        session.commit()
        return None

    offset, action = plan[0]

    idempotency_key = make_idempotency_key(payment.payment_id, attempt_number)
    payload = {
        "amount_paise": payment.amount_paise,
        "customer_name": f"Customer {customer.customer_id}",
        "customer_contact": SYNTHETIC_CONTACT_PLACEHOLDER,
        "description": f"Recovery action for {payment.payment_id} ({payment.decline_reason.value})",
    }
    return write_intent_with_state_change(
        session,
        payment.payment_id,
        idempotency_key,
        action.action_type.value,
        payload,
        now,
        due_at=now + offset,
    )
