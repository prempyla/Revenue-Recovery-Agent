"""Query layer over the execution event log: derives the shapes
full_agent.decide() expects — a failure log for the systemic detector, a
ContactTracker for compliance vetoes — from execution/'s own persisted
events. The detector (outage_detector.detect_systemic_event) is already a
pure function over a failure log; this module feeds it real data, it
doesn't touch it. No new coupling into simulator internals beyond the
Payment/Action/ContactTracker types full_agent already imports and expects
as input — this module reads execution/'s own tables and constructs those
same shapes, nothing more.

Schema gap found and CLOSED while building this (2026-08-25, DECISIONS.md):
the DIAGNOSED event's payload previously stored only decline_reason.
customer_id, amount_paise, instrument_type, and issuer_code were never
persisted anywhere in the execution event log at all, despite being
required fields (no defaults) on simulator.types.Payment — recent_failures()
below would have had no way to reconstruct a Payment without inventing an
issuer_code out of thin air. Fixed in orchestrator.py's DIAGNOSED payload,
which now records all of them — reported and closed, not synthesized.
failed_at is not stored a second time: the AT_RISK event's own event_time
already serves that purpose (orchestrator.diagnose_and_schedule writes both
from the same `now`, so this is consistent with how the rest of this
codebase already treats "failure time" and "diagnosis time" as the same
instant).

Schema gap found and NOT closed, reported instead: nothing in execution/
persists an explicit customer opt-out event. llm.reply_handling.
apply_reply_intent only mutates an ephemeral in-memory ContactTracker on an
OPT_OUT classification — it has no import of execution.* at all (by
design, checked structurally in tests/test_llm_reply_handling.py) and
never writes anything to this event log. So contact_tracker_for() below
correctly reconstructs contact_count (every customer-facing dispatch IS in
the log, via OutboxIntent + the EXECUTING event), but cannot reconstruct
opted_out — there is nothing persisted to read it back from. The tracker
this returns will never report a customer as opted out; that honestly
reflects what's actually recorded rather than fabricating a false negative,
but it does mean full_agent's explicit_opt_out veto is currently
unreachable through this query layer. Adding a persisted opt-out event type
to execution/ is a real gap for a future round, out of scope here.

outage_events, separately: full_agent.decide()'s outage_events parameter is
threaded through to _candidates_for but never actually read there (only
failure_log reaches detect_systemic_event) — confirmed by inspection, not
assumed. There is nothing to query for it; callers pass an empty list.
"""

from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy.orm import Session

from simulator.contact_tracking import ContactTracker, is_customer_facing
from simulator.types import Action, ActionType, DeclineReason, InstrumentType, Payment

from .eventlog import EventRecord
from .outbox import OutboxIntent
from .states import PaymentState

# ASSUMPTION: ungiven. Long enough to comfortably cover a systemic outage
# detection window + cooldown + full_agent's hold offset several times over
# (see simulator/full_agent.py's ASSUMED_MAX_OUTAGE_DURATION_MINUTES=100),
# short enough that this stays a bounded query as the log grows rather than
# an unbounded full-table scan. Not optimized further -- this layer, like
# the rest of this codebase, isn't built for production scale.
DEFAULT_FAILURE_LOOKBACK = timedelta(hours=6)

_REQUIRED_DIAGNOSED_FIELDS = ("customer_id", "amount_paise", "instrument_type", "issuer_code")


def _first_event_time(session: Session, payment_id: str, state: PaymentState) -> Optional[datetime]:
    row = (
        session.query(EventRecord)
        .filter_by(payment_id=payment_id, to_state=state.value)
        .order_by(EventRecord.id)
        .first()
    )
    return row.event_time if row else None


def recent_failures(
    session: Session, now: datetime, lookback: timedelta = DEFAULT_FAILURE_LOOKBACK
) -> List[Payment]:
    """Reconstructs a Payment for every payment DIAGNOSED within
    [now - lookback, now] — the failure_log shape
    simulator.outage_detector.detect_systemic_event (via full_agent.decide)
    expects. `now` is a parameter, never read from the clock, same
    discipline as decide() itself.

    Skips (does not fabricate) any DIAGNOSED event missing a required
    field, or any payment whose AT_RISK event can't be found — this only
    happens for events written before the 2026-08-25 payload fix, or a
    corrupted log; either way, the honest move is to leave that payment out
    of the failure log, not invent its issuer_code."""
    diagnosed_events = (
        session.query(EventRecord)
        .filter(EventRecord.to_state == PaymentState.DIAGNOSED.value)
        .filter(EventRecord.event_time >= now - lookback, EventRecord.event_time <= now)
        .order_by(EventRecord.id)
        .all()
    )

    failures = []
    for event in diagnosed_events:
        p = event.payload or {}
        if not all(field in p for field in _REQUIRED_DIAGNOSED_FIELDS):
            continue
        failed_at = _first_event_time(session, event.payment_id, PaymentState.AT_RISK)
        if failed_at is None:
            continue
        failures.append(
            Payment(
                payment_id=event.payment_id,
                customer_id=p["customer_id"],
                amount_paise=p["amount_paise"],
                instrument_type=InstrumentType(p["instrument_type"]),
                decline_reason=DeclineReason(p["decline_reason"]),
                issuer_code=p["issuer_code"],
                failed_at=failed_at,
            )
        )
    return failures


def _payment_ids_for_customer(session: Session, customer_id: str) -> List[str]:
    diagnosed_events = (
        session.query(EventRecord).filter(EventRecord.to_state == PaymentState.DIAGNOSED.value).all()
    )
    return [e.payment_id for e in diagnosed_events if (e.payload or {}).get("customer_id") == customer_id]


def contact_tracker_for(session: Session, customer_id: str) -> ContactTracker:
    """Reconstructs a ContactTracker for one customer: contact_count
    increments once per customer-facing OutboxIntent that actually reached
    EXECUTING (the worker genuinely attempted it — a merely SCHEDULED,
    not-yet-dispatched intent isn't a contact yet), replayed in
    chronological order, for payments belonging to this customer (matched
    via each payment's DIAGNOSED event payload).

    Does NOT reconstruct opted_out — see module docstring."""
    tracker = ContactTracker()

    entries = []
    for payment_id in _payment_ids_for_customer(session, customer_id):
        intent = (
            session.query(OutboxIntent).filter_by(payment_id=payment_id).order_by(OutboxIntent.id).first()
        )
        if intent is None:
            continue
        executing_at = _first_event_time(session, payment_id, PaymentState.EXECUTING)
        if executing_at is None:
            continue
        entries.append((executing_at, intent.action_type))

    entries.sort(key=lambda pair: pair[0])
    for _executing_at, action_type in entries:
        action = Action(ActionType(action_type))
        if is_customer_facing(action):
            tracker.record(customer_id, action)

    return tracker
