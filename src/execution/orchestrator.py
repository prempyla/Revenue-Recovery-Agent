"""Ties rules_only_policy + the state machine + the outbox into one path:
policy decides -> action persisted -> (separately) worker executes.

This round's declared single end-to-end route is INSUFFICIENT_FUNDS (or
MANDATE_INSUFFICIENT_FUNDS) -> rules_only routes to SEND_PAYMENT_LINK. Any
other action_type rules_only returns is treated as "not part of this round's
execution path" and abandoned with POLICY_STOP — not because the policy is
wrong, but because this layer only knows how to build a payload for
send_payment_link so far. The worker's own NotImplementedError (outbox.py)
is a separate, independent backstop for the outbox/worker layer in general,
tested directly rather than reached through this function.

simulator.types.Customer is a synthetic ground-truth persona with no real
name or phone number (by design — no PII in the simulator). customer_name/
customer_contact below are therefore clearly-synthetic placeholders, not
real contact info. Real notifications are left disabled in
RealRazorpayClient's payload (notify.sms/email = False in the future real
wiring) — see razorpay_client.py — so nothing gets sent to a fake number;
the returned short_url is what a human uses for manual verification.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from simulator.policies import rules_only_policy
from simulator.types import ActionType, Customer, DeclineReason, Payment

from .eventlog import append_event
from .idempotency import make_idempotency_key
from .outbox import OutboxIntent, write_intent_with_state_change
from .states import AbandonReason, PaymentState

SYNTHETIC_CONTACT_PLACEHOLDER = "9000000000"  # not a real number; notify is disabled


def diagnose_and_schedule(
    session: Session,
    payment: Payment,
    customer: Customer,
    now: datetime,
    attempt_number: int = 1,
) -> Optional[OutboxIntent]:
    """AT_RISK -> DIAGNOSED, then either SCHEDULED+outbox-intent (the
    SEND_PAYMENT_LINK route) or ABANDONED. Returns the OutboxIntent if one
    was scheduled, None if abandoned at diagnosis."""
    append_event(session, payment.payment_id, PaymentState.AT_RISK, now)
    append_event(
        session,
        payment.payment_id,
        PaymentState.DIAGNOSED,
        now,
        payload={"decline_reason": payment.decline_reason.value},
    )
    session.commit()

    plan = rules_only_policy(payment, customer)

    if not plan:
        append_event(
            session,
            payment.payment_id,
            PaymentState.ABANDONED,
            now,
            abandon_reason=AbandonReason.POLICY_STOP,
            payload={"reason": "rules_only returned no action for this category"},
        )
        session.commit()
        return None

    _offset, action = plan[0]

    if action.action_type != ActionType.SEND_PAYMENT_LINK:
        append_event(
            session,
            payment.payment_id,
            PaymentState.ABANDONED,
            now,
            abandon_reason=AbandonReason.POLICY_STOP,
            payload={
                "reason": (
                    f"rules_only routed to {action.action_type.value}, which "
                    "isn't part of this round's execution path (send_payment_link only)"
                )
            },
        )
        session.commit()
        return None

    idempotency_key = make_idempotency_key(payment.payment_id, attempt_number)
    payload = {
        "amount_paise": payment.amount_paise,
        "customer_name": f"Customer {customer.customer_id}",
        "customer_contact": SYNTHETIC_CONTACT_PLACEHOLDER,
        "description": f"Recovery link for {payment.payment_id} ({payment.decline_reason.value})",
    }
    return write_intent_with_state_change(
        session, payment.payment_id, idempotency_key, "send_payment_link", payload, now
    )
