"""The ONLY consumer of ReplyIntent in this codebase. No import of
razorpay_client or anything under execution/ — this module is structurally
incapable of moving money, not just tested to avoid it.

OPT_OUT sets an explicit flag (ContactTracker.mark_opted_out) with its own
hard veto in full_agent.decide() — a stated request, kept structurally
separate from the hidden persona annoyance_threshold (see
contact_tracking.py and DECISIONS.md 2026-08-25). PROMISE_TO_PAY schedules
a future re-evaluation THROUGH the existing decide() path — this function
calls it and returns whatever it decides, never executing anything itself.
DISPUTE / WRONG_PERSON / UNCLEAR: flagged for human review (out of scope
this round), no automated action.
"""

from datetime import datetime, timedelta
from typing import Optional, Sequence, Tuple

from simulator.contact_tracking import ContactTracker
from simulator.full_agent import decide
from simulator.types import Action, Customer, OutageEvent, Payment

from .types import ClassifiedReply, ReplyIntent

DEFAULT_REEVALUATION_DELAY_DAYS = 1  # ASSUMPTION: used when no date was parsed from the reply


def apply_reply_intent(
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
        return None

    if classified.intent == ReplyIntent.PROMISE_TO_PAY:
        if classified.promised_date is not None:
            reeval_time = datetime.combine(classified.promised_date, datetime.min.time())
        else:
            reeval_time = now + timedelta(days=DEFAULT_REEVALUATION_DELAY_DAYS)
        return decide(payment, customer, reeval_time, tracker, failure_log, outage_events)

    # DISPUTE / WRONG_PERSON / UNCLEAR: no automated action.
    return None
