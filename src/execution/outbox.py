"""Transactional outbox. The decision path writes the action intent in the
SAME transaction as the SCHEDULED state change (write_intent_with_state_change)
— so a crash between "decided" and "executed" can never lose the intent or
leave state and intent disagreeing about whether an action was committed to.
A separate worker (run_outbox_worker_once) picks up pending intents that are
DUE and executes them: at-least-once delivery, protected against
double-execution by our own idempotency_key unique constraint plus the
pre-API EXECUTING commit (see razorpay_client.py for why we can't lean on
Razorpay's own idempotency support for Payment Links — it doesn't have any).

due_at (2026-08-25 audit fix #2/#8): a decision's offset from decide()
previously had nowhere to land — every intent fired the instant a worker
ran, regardless of what the policy actually decided. write_intent_with_
state_change now takes an explicit due_at; the worker filters on it, with
`now` passed in as a parameter, same discipline as decide() (no clock reads
inside).

Dispatch (2026-08-25, same fix): SEND_PAYMENT_LINK is the only action_type
with a real Razorpay test-mode primitive behind it. Every other action_type
full_agent can emit (retry_now, retry_scheduled, send_nudge,
send_instrument_update_link, escalate_alternate_instrument) has no such
primitive wired up, and is dispatched as a LOGGED SIMULATED SEND instead —
explicitly tagged `simulated: True` in the result/audit payload, never
presented as a real send. An action_type genuinely outside this set still
raises NotImplementedError, unchanged from before.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from .db import Base
from .eventlog import append_event
from .razorpay_client import RazorpayClientInterface
from .states import AbandonReason, PaymentState

# action_types with no real Razorpay test-mode primitive behind them yet --
# dispatched as a logged simulated send (see _execute_simulated_action)
# rather than a real API call.
SIMULATED_ACTION_TYPES = frozenset(
    {"retry_now", "retry_scheduled", "send_nudge", "send_instrument_update_link", "escalate_alternate_instrument"}
)


class OutboxIntent(Base):
    __tablename__ = "outbox_intents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    action_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
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
    due_at: Optional[datetime] = None,
) -> OutboxIntent:
    """DIAGNOSED -> SCHEDULED, plus the outbox intent row, committed together.
    This IS the outbox guarantee — everything below is one transaction.

    due_at defaults to event_time (due immediately) when not given, so every
    pre-existing caller that never specified a delay keeps behaving exactly
    as before. orchestrator.py now always passes an explicit due_at computed
    from the policy's decided offset."""
    if due_at is None:
        due_at = event_time
    append_event(session, payment_id, PaymentState.SCHEDULED, event_time)
    intent = OutboxIntent(
        payment_id=payment_id,
        idempotency_key=idempotency_key,
        action_type=action_type,
        payload=payload,
        status="pending",
        created_at=event_time,
        due_at=due_at,
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


def _execute_simulated_action(intent: OutboxIntent, client: RazorpayClientInterface) -> dict:
    """No Razorpay test-mode primitive exists for this action_type. Logs a
    simulated send and returns a result explicitly tagged simulated=True --
    never makes a real API call, never claims to have sent anything real.
    `client` is accepted (matching the dispatch signature) but deliberately
    unused."""
    return {
        "simulated": True,
        "action_type": intent.action_type,
        "note": (
            "no Razorpay test-mode primitive wired for this action_type; "
            "logged as a simulated send, not executed against any real API"
        ),
    }


_DISPATCH = {
    "send_payment_link": _execute_send_payment_link,
    **{action_type: _execute_simulated_action for action_type in SIMULATED_ACTION_TYPES},
}


def run_outbox_worker_once(
    session: Session, client: RazorpayClientInterface, now: datetime
) -> list[OutboxIntent]:
    """Single pass: process every currently-pending, currently-DUE intent
    (status="pending" AND due_at <= now). Rows not yet due are skipped, not
    errored -- they'll be picked up by a later pass once due_at arrives.
    `now` is a parameter, never read from the clock inside, same discipline
    as decide(). Not a `while True` loop itself — that's what makes this
    deterministic and testable; a real deployment wraps this in a poll loop
    with a sleep."""
    pending = (
        session.query(OutboxIntent)
        .filter(OutboxIntent.status == "pending", OutboxIntent.due_at <= now)
        .order_by(OutboxIntent.id)
        .all()
    )
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
