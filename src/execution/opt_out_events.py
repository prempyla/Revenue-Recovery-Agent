"""Persisted, customer-scoped opt-out record.

Deliberately NOT part of the payment-scoped EventRecord/PaymentState FSM:
an opt-out is a fact about a CUSTOMER — it applies across all of their
payments, past and future — not a transition in any single payment's
lifecycle. Forcing it into the payment-scoped log would mean picking one
"representative" payment_id to hang it on and having every other query
walk that payment's history to find it out — a workaround, not a fit for
the concept. This is a dedicated small append-only table instead, same
philosophy as EventRecord (never updated, never deleted; effective state
is derived by reading the earliest row), scoped to the thing that actually
owns this fact.

2026-08-25 — fourth instance of the same family logged in DECISIONS.md:
before this file existed, an explicit opt-out lived exclusively in an
in-memory ContactTracker (llm.reply_handling.apply_reply_intent's
tracker.mark_opted_out call) — correct behavior for the rest of that one
process's lifetime, but it never survived a restart. Customer says stop,
process restarts, the system contacts them again — a guardrail that only
exists in memory isn't a guardrail. This table is what makes it durable:
apply_reply_intent now writes here directly, and
query_layer.contact_tracker_for() reads it back on every call, so the veto
holds after any restart, not just within the process that recorded it.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from .db import Base


class CustomerOptOutEvent(Base):
    __tablename__ = "customer_opt_out_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    reply_intent: Mapped[str] = mapped_column(String, nullable=False)
    source_payment_id: Mapped[str] = mapped_column(String, nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)


def record_opt_out(
    session: Session,
    customer_id: str,
    reply_intent: str,
    source_payment_id: str,
    event_time: datetime,
) -> CustomerOptOutEvent:
    """Append-only: every OPT_OUT reply gets its own row, never updated or
    deduplicated at write time — that's what makes this a real log, not a
    mutable flag. earliest_opt_out() below is what determines the
    effective (first) opt-out moment, matching
    ContactTracker.mark_opted_out's existing "first wins" semantics."""
    event = CustomerOptOutEvent(
        customer_id=customer_id,
        reply_intent=reply_intent,
        source_payment_id=source_payment_id,
        event_time=event_time,
    )
    session.add(event)
    session.commit()
    return event


def earliest_opt_out(session: Session, customer_id: str) -> Optional[datetime]:
    row = (
        session.query(CustomerOptOutEvent)
        .filter_by(customer_id=customer_id)
        .order_by(CustomerOptOutEvent.event_time)
        .first()
    )
    return row.event_time if row else None
