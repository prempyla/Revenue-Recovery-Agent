"""Transactional outbox. The decision path writes the action intent in the
SAME transaction as the SCHEDULED state change (write_intent_with_state_change)
— so a crash between "decided" and "executed" can never lose the intent or
leave state and intent disagreeing about whether an action was committed to.
A separate worker (run_outbox_worker_once) picks up pending intents and
executes them: at-least-once delivery, protected against double-execution by
our own idempotency_key unique constraint plus the pre-API EXECUTING commit
(see razorpay_client.py for why we can't lean on Razorpay's own idempotency
support for Payment Links — it doesn't have any).

Only SEND_PAYMENT_LINK is wired to a real dispatch this round. Any other
action_type reaching the worker raises NotImplementedError — same pattern as
full_agent: not silently faked, explicitly not built yet.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from .db import Base
from .eventlog import append_event
from .razorpay_client import RazorpayClientInterface
from .states import AbandonReason, PaymentState


class OutboxIntent(Base):
    __tablename__ = "outbox_intents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    action_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String, nullable=True)


def write_intent_with_state_change(
    session: Session,
    payment_id: str,
    idempotency_key: str,
    action_type: str,
    payload: dict,
    event_time: datetime,
) -> OutboxIntent:
    """DIAGNOSED -> SCHEDULED, plus the outbox intent row, committed together.
    This IS the outbox guarantee — everything below is one transaction."""
    append_event(session, payment_id, PaymentState.SCHEDULED, event_time)
    intent = OutboxIntent(
        payment_id=payment_id,
        idempotency_key=idempotency_key,
        action_type=action_type,
        payload=payload,
        status="pending",
        created_at=event_time,
    )
    session.add(intent)
    session.commit()
    return intent


def _execute_send_payment_link(intent: OutboxIntent, client: RazorpayClientInterface) -> dict:
    p = intent.payload
    result = client.create_payment_link(
        amount_paise=p["amount_paise"],
        customer_name=p["customer_name"],
        customer_contact=p["customer_contact"],
        reference_id=intent.idempotency_key,
        description=p.get("description", ""),
    )
    return dict(result)


_DISPATCH = {
    "send_payment_link": _execute_send_payment_link,
}


def run_outbox_worker_once(
    session: Session, client: RazorpayClientInterface, now: datetime
) -> list[OutboxIntent]:
    """Single pass: process every currently-pending intent. Not a `while
    True` loop itself — that's what makes this deterministic and testable;
    a real deployment wraps this in a poll loop with a sleep."""
    pending = session.query(OutboxIntent).filter_by(status="pending").order_by(OutboxIntent.id).all()
    processed = []

    for intent in pending:
        # Commit the EXECUTING transition BEFORE calling the API: if the
        # process crashes during the call, the payment is visibly stuck in
        # EXECUTING (a known gap this round — that's what the excluded
        # reconciliation poller is for) rather than silently lost.
        append_event(session, intent.payment_id, PaymentState.EXECUTING, now)
        session.commit()

        handler = _DISPATCH.get(intent.action_type)
        if handler is None:
            raise NotImplementedError(
                f"outbox worker has no dispatch for action_type={intent.action_type!r}"
            )

        try:
            result = handler(intent, client)
        except Exception as exc:
            intent.status = "failed"
            intent.error = str(exc)
            append_event(
                session,
                intent.payment_id,
                PaymentState.ABANDONED,
                now,
                abandon_reason=AbandonReason.EXECUTION_ERROR,
                payload={"error": str(exc)},
            )
            session.commit()
            processed.append(intent)
            continue

        intent.status = "done"
        intent.result = result
        intent.executed_at = now
        append_event(session, intent.payment_id, PaymentState.AWAITING_CONFIRMATION, now, payload=result)
        session.commit()
        processed.append(intent)

    return processed
