"""Append-only event log. Current state is DERIVED by replay, never stored
and mutated in place — no `payments.status` column anywhere in this layer.

derive_state() doesn't just read the last row and trust it; it folds the
full ordered sequence through validate_transition() again, so a corrupted
or hand-edited event sequence is caught at read time, not silently believed.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from .clock import UTCDateTime
from .db import Base
from .states import AbandonReason, IllegalStateTransition, PaymentState, validate_transition


class EventRecord(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    from_state: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    to_state: Mapped[str] = mapped_column(String, nullable=False)
    abandon_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    event_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


def _events_for_payment(session: Session, payment_id: str) -> list[EventRecord]:
    return (
        session.query(EventRecord)
        .filter_by(payment_id=payment_id)
        .order_by(EventRecord.id.asc())
        .all()
    )


def append_event(
    session: Session,
    payment_id: str,
    to_state: PaymentState,
    event_time: datetime,
    abandon_reason: Optional[AbandonReason] = None,
    payload: Optional[dict] = None,
) -> EventRecord:
    """Validates against the CURRENT derived state, then adds the row to the
    session (does not commit — caller controls the transaction boundary, so
    this can be combined with an outbox write in one commit)."""
    current_state = derive_state(session, payment_id)
    validate_transition(current_state, to_state, abandon_reason)

    record = EventRecord(
        payment_id=payment_id,
        from_state=current_state.value if current_state else None,
        to_state=to_state.value,
        abandon_reason=abandon_reason.value if abandon_reason else None,
        event_time=event_time,
        payload=payload,
    )
    session.add(record)
    return record


def derive_state(session: Session, payment_id: str) -> Optional[PaymentState]:
    """Replays every event for payment_id in order, re-validating each
    transition. Returns None if the payment has no events yet."""
    events = _events_for_payment(session, payment_id)
    state: Optional[PaymentState] = None
    for event in events:
        to_state = PaymentState(event.to_state)
        abandon_reason = AbandonReason(event.abandon_reason) if event.abandon_reason else None
        validate_transition(state, to_state, abandon_reason)
        state = to_state
    return state


def history(session: Session, payment_id: str) -> list[EventRecord]:
    """Full ordered event history for a payment — the audit trail."""
    return _events_for_payment(session, payment_id)
