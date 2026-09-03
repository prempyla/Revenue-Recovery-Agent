"""The ONLY consumer of ReplyIntent in this codebase.

Narrowed guarantee, 2026-08-25 (see DECISIONS.md): this module never
imports outbox.py or razorpay_client.py — the two modules that can move
money or touch a real/fake payment API — so it is structurally incapable
of executing a payment action, not just tested to avoid it. It DOES now
import execution.opt_out_events to durably PERSIST the opt-out fact
(an audit-only table, no PaymentState/FSM involvement, no money movement)
— closing the gap where an opt-out lived only in an in-memory
ContactTracker and vanished on restart. The previous, broader claim ("no
import of anything under execution/") was stricter than what actually
needed guaranteeing; this is the corrected, still-meaningful version of
it, not a loosening for convenience.

OPT_OUT sets the explicit flag in-memory (ContactTracker.mark_opted_out,
for immediate effect within this process) AND persists it
(execution.opt_out_events.record_opt_out, for effect that survives a
restart) — a stated request, kept structurally separate from the hidden
persona annoyance_threshold (see contact_tracking.py and DECISIONS.md
2026-08-25). PROMISE_TO_PAY schedules a future re-evaluation THROUGH the
existing decide() path — this function calls it and returns whatever it
decides, never executing anything itself. DISPUTE / WRONG_PERSON / UNCLEAR:
flagged for human review (out of scope this round), no automated action.
"""

from datetime import datetime, timedelta
from typing import Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from simulator.contact_tracking import ContactTracker
from simulator.full_agent import decide
from simulator.types import Action, Customer, OutageEvent, Payment

from execution.opt_out_events import record_opt_out

from .types import ClassifiedReply, ReplyIntent

DEFAULT_REEVALUATION_DELAY_DAYS = 1  # ASSUMPTION: used when no date was parsed from the reply


def apply_reply_intent(
    session: Session,
    classified: ClassifiedReply,
    payment: Payment,
    customer: Customer,
    tracker: ContactTracker,
    failure_log: Sequence[Payment],
    outage_events: Sequence[OutageEvent],
    now: datetime,
) -> Optional[Tuple[timedelta, Action]]:
    if classified.intent == ReplyIntent.OPT_OUT:
        tracker.mark_opted_out(customer.customer_id, now)
        record_opt_out(
            session,
            customer_id=customer.customer_id,
            reply_intent=classified.intent.value,
            source_payment_id=payment.payment_id,
            event_time=now,
        )
        return None

    if classified.intent == ReplyIntent.PROMISE_TO_PAY:
        if classified.promised_date is not None:
            # now.timetz(), not datetime.min.time() (found while fixing
            # decide()'s now to be genuinely load-bearing, 2026-08-27 -- see
            # INCIDENTS.md), for two independent reasons:
            #   1. datetime.min.time() is midnight with no tzinfo. decide()
            #      now does max(now, payment.failed_at + offset), which
            #      raises TypeError comparing a naive reeval_time against an
            #      execution/-sourced aware payment.failed_at -- harmless
            #      while decide() ignored `now`, not once it's compared.
            #      now.timetz() carries the same tzinfo as `now` itself, so
            #      reeval_time stays in whatever aware/naive domain the
            #      caller is already using.
            #   2. Midnight is virtually never inside a customer's declared
            #      contact_hours, so ALWAYS re-evaluating at 00:00 on the
            #      promised date would make full_agent.decide()'s
            #      contact-hours veto fire on nearly every real
            #      promise-to-pay reply, regardless of what was promised --
            #      not a deliberate design choice, just untested until
            #      `now` started mattering. Reusing now's hour keeps the
            #      re-evaluation at the same time of day real outreach
            #      already happens at, same as the no-date fallback below
            #      already does via `now + timedelta(days=...)`.
            reeval_time = datetime.combine(classified.promised_date, now.timetz())
        else:
            reeval_time = now + timedelta(days=DEFAULT_REEVALUATION_DELAY_DAYS)
        return decide(payment, customer, reeval_time, tracker, failure_log, outage_events)

    # DISPUTE / WRONG_PERSON / UNCLEAR: no automated action.
    return None
