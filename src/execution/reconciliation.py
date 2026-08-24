"""Reconciliation poller: the backstop for the EXECUTING-stuck gap left open
deliberately in outbox.py ("if the process crashes during the API call, the
payment is stuck in EXECUTING with no automatic recovery -- that's what the
reconciliation poller is for"). Also closes the equivalent gap for
AWAITING_CONFIRMATION: a payment whose webhook was never delivered (Razorpay
retries, but not forever -- see docs/manual_webhook_verification.md) would
otherwise wait indefinitely with nothing to unstick it.

Same testability discipline as outbox.run_outbox_worker_once: a single
deterministic pass over currently-stale payments, not a bare `while True` --
a real deployment wraps this in its own poll loop with a sleep.

Every reconciled transition is tagged in its event payload with
"source": "reconciliation_poll", distinct from webhook.py's
"source": "webhook" tag -- the audit trail records HOW a payment's outcome
was learned, not just what it turned out to be.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from . import config
from .eventlog import append_event, history
from .outbox import OutboxIntent
from .razorpay_client import PaymentLinkResult, RazorpayClientInterface
from .states import AbandonReason, PaymentState

STALE_STATES = (PaymentState.EXECUTING, PaymentState.AWAITING_CONFIRMATION)
TERMINAL_REMOTE_FAILURE_STATUSES = ("expired", "cancelled")


@dataclass(frozen=True)
class ReconciliationResult:
    payment_id: str
    previous_state: PaymentState
    new_state: Optional[PaymentState]  # None if left unchanged (still genuinely pending)
    remote_status: Optional[str]
    action: str


def _stale_payment_ids(session: Session, now: datetime, staleness_minutes: int) -> List[str]:
    payment_ids = [row[0] for row in session.query(OutboxIntent.payment_id).distinct()]
    stale = []
    for payment_id in payment_ids:
        events = history(session, payment_id)
        if not events:
            continue
        last_event = events[-1]
        current_state = PaymentState(last_event.to_state)
        if current_state not in STALE_STATES:
            continue
        if now - last_event.event_time >= timedelta(minutes=staleness_minutes):
            stale.append(payment_id)
    return stale


def _latest_intent(session: Session, payment_id: str) -> Optional[OutboxIntent]:
    return (
        session.query(OutboxIntent)
        .filter_by(payment_id=payment_id)
        .order_by(OutboxIntent.id.desc())
        .first()
    )


def _record_intent_result(intent: OutboxIntent, remote: PaymentLinkResult) -> None:
    intent.status = "done"
    intent.result = dict(remote)


def run_reconciliation_poll_once(
    session: Session,
    client: RazorpayClientInterface,
    now: datetime,
    staleness_minutes: int = config.RECONCILIATION_STALENESS_MINUTES,
) -> List[ReconciliationResult]:
    """Single pass: find payments stuck in EXECUTING/AWAITING_CONFIRMATION
    past staleness_minutes, ask Razorpay what actually happened, and
    reconcile local state to match."""
    results: List[ReconciliationResult] = []

    for payment_id in _stale_payment_ids(session, now, staleness_minutes):
        original_state = PaymentState(history(session, payment_id)[-1].to_state)
        intent = _latest_intent(session, payment_id)
        if intent is None:
            continue  # shouldn't happen given how orchestrator/outbox work; defensive skip

        remote = client.fetch_payment_link_status(intent.idempotency_key)
        remote_status = remote["status"] if remote else None
        base_payload: Dict = {"source": "reconciliation_poll", "remote_status": remote_status}
        if remote:
            base_payload["remote"] = dict(remote)

        if remote is None:
            # No record on Razorpay's side at all -- whatever we attempted
            # never completed there either. Nothing to recover from; give up
            # rather than silently auto-retry (out of scope this round).
            append_event(
                session,
                payment_id,
                PaymentState.ABANDONED,
                now,
                abandon_reason=AbandonReason.EXECUTION_ERROR,
                payload={**base_payload, "reason": "no_remote_record_found"},
            )
            session.commit()
            results.append(
                ReconciliationResult(payment_id, original_state, PaymentState.ABANDONED, None, "abandoned_no_record")
            )
            continue

        # If we were stuck in EXECUTING but the API call actually did land on
        # Razorpay's side, catch local state up to AWAITING_CONFIRMATION
        # before applying whatever the remote status implies next -- the FSM
        # doesn't allow EXECUTING -> RECOVERED/ABANDONED directly.
        if original_state == PaymentState.EXECUTING:
            append_event(session, payment_id, PaymentState.AWAITING_CONFIRMATION, now, payload=base_payload)
            _record_intent_result(intent, remote)
            session.commit()

        if remote_status == "paid":
            append_event(session, payment_id, PaymentState.RECOVERED, now, payload=base_payload)
            session.commit()
            results.append(
                ReconciliationResult(payment_id, original_state, PaymentState.RECOVERED, remote_status, "reconciled_to_recovered")
            )
        elif remote_status in TERMINAL_REMOTE_FAILURE_STATUSES:
            append_event(
                session,
                payment_id,
                PaymentState.ABANDONED,
                now,
                abandon_reason=AbandonReason.PAYMENT_FAILED,
                payload=base_payload,
            )
            session.commit()
            results.append(
                ReconciliationResult(payment_id, original_state, PaymentState.ABANDONED, remote_status, "reconciled_to_abandoned")
            )
        else:
            # Still genuinely pending (e.g. "created", not yet paid) -- not
            # actually stuck, just still waiting. No further event logged
            # beyond the EXECUTING->AWAITING_CONFIRMATION catch-up (if any)
            # already committed above: the event log records transitions,
            # not "we checked and nothing changed."
            action = (
                "caught_up_to_awaiting_confirmation"
                if original_state == PaymentState.EXECUTING
                else "still_pending"
            )
            new_state = PaymentState.AWAITING_CONFIRMATION if original_state == PaymentState.EXECUTING else None
            results.append(ReconciliationResult(payment_id, original_state, new_state, remote_status, action))

    return results
