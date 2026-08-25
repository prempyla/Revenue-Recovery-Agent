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

Worker lease (2026-08-25 P1 #2, DECISIONS.md): before this, run_outbox_
worker_once's SELECT of pending+due rows had no claim step at all. Two
concurrent worker processes would both fetch the same rows and both
execute them — Razorpay's reference_id uniqueness would catch the
resulting duplicate as an ERROR after the fact, not prevent it. claimed_by
/claimed_until turn "fetch pending rows" into "atomically claim a batch,
then only process what was actually won": _claim_batch's UPDATE ... WHERE
re-checks eligibility (status="pending" AND due_at<=now AND
(claimed_until IS NULL OR claimed_until < now)) as part of the same
statement that sets the claim, so a row a concurrent writer already
claimed simply matches zero rows here instead of being claimed twice.
SQLite's own coarse, single-writer-at-a-time locking is what makes this
straightforward to implement correctly with a plain conditional UPDATE;
on Postgres the equivalent (and the mechanism this is deliberately
mirroring, not reinventing) is `SELECT ... FOR UPDATE SKIP LOCKED`. An
expired lease (claimed_until < now, e.g. a worker that claimed a batch
and then crashed) is reclaimable by a later call — the dead-worker
recovery case, and the one that actually justifies a lease *expiry*
rather than a permanent claim.

NOT a perfect equivalent to FOR UPDATE SKIP LOCKED, and worth being
honest about the gap (found by deliberately stress-testing the mechanism
after it shipped, not assumed away -- see DECISIONS.md): a Postgres row
lock is held for the life of the transaction, so a live-but-slow worker
never loses it just because time passed. This lease is time-based, not
transaction-based -- if one worker's actual processing (the handler call,
a real network round-trip in production) takes longer than
lease_duration, a second worker can correctly reclaim the row believing
the first is dead, when it's actually just slow. _process_claimed_intents
below catches this specific case: the stale worker's own
append_event(EXECUTING) on a row another worker has already moved past
EXECUTING raises IllegalStateTransition (the FSM's own transition
validation is the actual backstop that prevents a genuine second Razorpay
call here, not the lease alone) -- caught, and that one intent is skipped
rather than crashing the rest of the stale worker's batch. The real
mitigation is sizing DEFAULT_LEASE_DURATION comfortably above realistic
processing time, same as any lease-based system.
"""

from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import JSON, Integer, String, or_, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from .clock import UTCDateTime
from .db import Base
from .eventlog import append_event
from .razorpay_client import RazorpayClientInterface
from .states import AbandonReason, IllegalStateTransition, PaymentState

# action_types with no real Razorpay test-mode primitive behind them yet --
# dispatched as a logged simulated send (see _execute_simulated_action)
# rather than a real API call.
SIMULATED_ACTION_TYPES = frozenset(
    {"retry_now", "retry_scheduled", "send_nudge", "send_instrument_update_link", "escalate_alternate_instrument"}
)

# ASSUMPTION: ungiven. Generous relative to how long a single dispatch
# (one Razorpay call or one logged simulated send) actually takes, so a
# live worker's own claim never expires out from under it mid-batch; short
# enough that a genuinely dead worker's claims become reclaimable well
# before anyone would notice the delay.
DEFAULT_LEASE_DURATION = timedelta(minutes=5)

# ASSUMPTION: ungiven. Bounded so one worker claiming a batch can't starve
# every other concurrent worker out of the entire pending set indefinitely.
DEFAULT_CLAIM_BATCH_SIZE = 50


class OutboxIntent(Base):
    __tablename__ = "outbox_intents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    action_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    executed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    claimed_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    claimed_until: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)


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


def _eligible_clause(now: datetime):
    """status="pending" AND due_at <= now AND (claimed_until IS NULL OR
    claimed_until < now) -- an unclaimed-or-lease-expired, currently-due,
    still-pending row. Shared between the candidate SELECT and each claim
    UPDATE's WHERE so both sides agree on what "eligible" means."""
    return (
        OutboxIntent.status == "pending",
        OutboxIntent.due_at <= now,
        or_(OutboxIntent.claimed_until.is_(None), OutboxIntent.claimed_until < now),
    )


def _claim_batch(
    session: Session,
    worker_id: str,
    now: datetime,
    lease_duration: timedelta,
    batch_size: int,
) -> List[OutboxIntent]:
    """Atomically claims up to batch_size eligible intents for worker_id.
    Portable equivalent of Postgres's SELECT ... FOR UPDATE SKIP LOCKED,
    not a reinvention of it: SQLite has no such clause, but its coarse
    single-writer-at-a-time locking means a plain conditional UPDATE is
    enough to get the same guarantee. Each row's UPDATE re-checks
    eligibility as part of the SAME statement that sets the claim, so if a
    concurrent writer already claimed it between this function's candidate
    SELECT and this specific UPDATE, the WHERE clause matches zero rows
    here and the row is silently skipped rather than claimed twice --
    checked via rowcount, not assumed. See module docstring for why the
    lease has an EXPIRY rather than being a permanent claim (a crashed
    worker's rows must eventually become reclaimable)."""
    candidate_ids = [
        row[0]
        for row in session.query(OutboxIntent.id)
        .filter(*_eligible_clause(now))
        .order_by(OutboxIntent.id)
        .limit(batch_size)
        .all()
    ]

    lease_expiry = now + lease_duration
    claimed_ids = []
    for intent_id in candidate_ids:
        result = session.execute(
            update(OutboxIntent)
            .where(OutboxIntent.id == intent_id, *_eligible_clause(now))
            .values(claimed_by=worker_id, claimed_until=lease_expiry)
        )
        if result.rowcount == 1:
            claimed_ids.append(intent_id)
    session.commit()

    if not claimed_ids:
        return []
    return (
        session.query(OutboxIntent)
        .filter(OutboxIntent.id.in_(claimed_ids))
        .order_by(OutboxIntent.id)
        .all()
    )


def _process_claimed_intents(
    session: Session, client: RazorpayClientInterface, now: datetime, claimed: List[OutboxIntent]
) -> List[OutboxIntent]:
    """Executes exactly the intents this worker already won via
    _claim_batch -- split out from run_outbox_worker_once specifically so
    a test can interleave two workers' claim calls before either one
    processes anything, which is the actual race window a lease exists to
    close (see tests/test_execution_outbox_lease.py).

    Found by deliberately stress-testing the lease after it shipped, not
    requested: this worker's OWN claim can be stale by the time it gets
    here -- if it took longer than lease_duration to work through an
    earlier intent in this same batch, a second worker may have correctly
    (from ITS perspective) reclaimed and already finished a later one.
    That shows up here as append_event(EXECUTING) raising
    IllegalStateTransition, since the payment has already moved past
    EXECUTING under the other worker. Caught below: skip that one stale
    intent (never call the handler for it -- no second real dispatch) and
    keep processing the rest of this worker's batch, rather than one
    reclaimed row taking down the whole pass with an unhandled exception."""
    processed = []

    for intent in claimed:
        # Commit the EXECUTING transition BEFORE calling the API: if the
        # process crashes during the call, the payment is visibly stuck in
        # EXECUTING (a known gap this round — that's what the excluded
        # reconciliation poller is for) rather than silently lost.
        try:
            append_event(session, intent.payment_id, PaymentState.EXECUTING, now)
            session.commit()
        except IllegalStateTransition:
            # Lost this one to another worker after our claim went stale
            # -- see module docstring. Nothing was added to the session
            # (validate_transition raises before the event is constructed),
            # so there's nothing to roll back; just don't process it.
            continue

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
        # Simulated sends (result["simulated"] is True -- see
        # _execute_simulated_action) go straight to the terminal
        # SIMULATED_SENT, never AWAITING_CONFIRMATION: nothing can ever
        # confirm a send that never happened, so waiting on a confirmation
        # that will never arrive would be a false statement in the audit
        # trail, not just an indefinite wait. See states.py's
        # SIMULATED_SENT docstring and DECISIONS.md 2026-08-25.
        next_state = (
            PaymentState.SIMULATED_SENT if result.get("simulated") else PaymentState.AWAITING_CONFIRMATION
        )
        append_event(session, intent.payment_id, next_state, now, payload=result)
        session.commit()
        processed.append(intent)

    return processed


def run_outbox_worker_once(
    session: Session,
    client: RazorpayClientInterface,
    now: datetime,
    worker_id: str,
    lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    batch_size: int = DEFAULT_CLAIM_BATCH_SIZE,
) -> list[OutboxIntent]:
    """Single pass: claim a batch of currently-pending, currently-DUE,
    currently-unclaimed-or-lease-expired intents for worker_id, then
    process only what was actually claimed. Rows not yet due, or already
    claimed by a live lease, are skipped, not errored -- they'll be picked
    up by a later pass (this worker or another) once due_at arrives or the
    lease expires. `now` and `worker_id` are both parameters, never read
    from the clock or generated inside, same discipline as decide(). Not a
    `while True` loop itself — that's what makes this deterministic and
    testable; a real deployment wraps this in a poll loop with a sleep and
    a real worker identity (hostname+pid, a UUID, etc.)."""
    claimed = _claim_batch(session, worker_id, now, lease_duration, batch_size)
    return _process_claimed_intents(session, client, now, claimed)
