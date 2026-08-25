"""Webhook receiver. Verify HMAC signature, dedupe on event id, ack fast,
process async.

Two SDK behaviors worth flagging up front, verified against the installed
`razorpay` package rather than assumed (see chat for the check):
  - razorpay.Utility.verify_webhook_signature(body, signature, secret)
    requires `body` as a `str`, not `bytes` -- passing raw request bytes
    raises TypeError, not a clean signature failure. We decode the raw body
    with .decode("utf-8") before calling it, and verify on the RAW string
    (never on a re-serialized/parsed-then-dumped body, which is not
    guaranteed byte-identical and would break the signature).
  - It raises SignatureVerificationError on mismatch rather than returning
    False. verify_signature() below catches that and returns bool.

Verified 2026-08-24 against 6 real webhook deliveries captured during manual
verification (docs/manual_webhook_verification.md; fixtures in
tests/fixtures/real_webhook_*.json):
  - payload.payment_link.entity.reference_id was correctly assumed --
    that structural shape needed no change.
  - The event id location was WRONG: it assumed payload["id"] or
    payload["event_id"] in the JSON body. Real deliveries never carry an id
    in the body at all -- it's only ever in the X-Razorpay-Event-Id header.
    Every real webhook received during verification got rejected with 400
    "missing event id" until this was fixed to read the header.
  - Razorpay retries a failing webhook repeatedly (visible in the captured
    requests: payment.failed/authorized/captured/order.paid/
    payment_link.paid each delivered multiple times while every attempt was
    400ing) -- confirms at-least-once delivery is real, not just a spec
    claim, and that is_duplicate_event()'s dedup earns its place here.

P1 timezone fix (2026-08-25, DECISIONS.md): the two real-clock reads here
(mark_event_seen, process_webhook_event's background call) used to be bare
`datetime.now()` -- host-local time, not guaranteed to be IST or even UTC.
Now `.clock.utc_now()`, explicit and timezone-aware. See clock.py for the
full writeup and simulator/timezones.py for the other half (converting a
UTC timestamp back to IST for the business rules that actually need it).
"""

import json
from datetime import datetime
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from sqlalchemy import String
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.orm import sessionmaker

from .clock import UTCDateTime, utc_now
from .db import Base
from .eventlog import append_event
from .states import AbandonReason, PaymentState


class SeenWebhookEvent(Base):
    __tablename__ = "seen_webhook_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    import razorpay

    utility = razorpay.Utility()
    try:
        return utility.verify_webhook_signature(raw_body.decode("utf-8"), signature, secret)
    except razorpay.errors.SignatureVerificationError:
        return False


def is_duplicate_event(session: Session, event_id: str) -> bool:
    return session.get(SeenWebhookEvent, event_id) is not None


def mark_event_seen(session: Session, event_id: str, now: datetime) -> None:
    session.add(SeenWebhookEvent(event_id=event_id, received_at=now))
    session.commit()


def _payment_id_from_reference_id(reference_id: str) -> str:
    # idempotency.make_idempotency_key() format: "{payment_id}:attempt:{n}"
    return reference_id.split(":attempt:")[0]


def process_webhook_event(session: Session, payload: dict, now: datetime) -> Optional[str]:
    """Applies the AWAITING_CONFIRMATION -> {RECOVERED, ABANDONED} transition
    for the payment this event refers to. Returns the payment_id acted on,
    or None if the event isn't one we act on.

    Assumed envelope (unverified against a live webhook -- see module
    docstring): {"event": "payment_link.paid" | "payment_link.expired" | ...,
    "payload": {"payment_link": {"entity": {"reference_id": ...}}}}
    """
    event = payload.get("event")
    entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
    reference_id = entity.get("reference_id")
    if not reference_id:
        return None

    payment_id = _payment_id_from_reference_id(reference_id)
    # Tagged so the audit trail distinguishes "we learned this from a
    # webhook" from reconciliation.py's "source": "reconciliation_poll".
    tagged_payload = {"source": "webhook", "entity": entity}

    if event == "payment_link.paid":
        append_event(session, payment_id, PaymentState.RECOVERED, now, payload=tagged_payload)
        session.commit()
        return payment_id

    if event in ("payment_link.expired", "payment_link.cancelled"):
        append_event(
            session,
            payment_id,
            PaymentState.ABANDONED,
            now,
            abandon_reason=AbandonReason.PAYMENT_FAILED,
            payload=tagged_payload,
        )
        session.commit()
        return payment_id

    return None


def create_app(session_factory: sessionmaker, webhook_secret: str) -> FastAPI:
    app = FastAPI()

    @app.post("/webhooks/razorpay")
    async def receive_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
        x_razorpay_signature: str = Header(default=""),
        x_razorpay_event_id: str = Header(default=""),
    ):
        raw_body = await request.body()

        if not verify_signature(raw_body, x_razorpay_signature, webhook_secret):
            raise HTTPException(status_code=400, detail="invalid signature")

        # Verified against 6 real webhook deliveries (see
        # docs/manual_webhook_verification.md): the event id is ONLY ever in
        # the X-Razorpay-Event-Id header. It is never present in the JSON
        # body under "id" or "event_id" -- an earlier assumption that every
        # single real delivery during manual verification hit and got
        # rejected for. Trusting the header, not the body, for this.
        event_id = x_razorpay_event_id
        if not event_id:
            raise HTTPException(status_code=400, detail="missing event id")

        payload = json.loads(raw_body)

        session = session_factory()
        try:
            if is_duplicate_event(session, event_id):
                return {"status": "ok", "duplicate": True}
            mark_event_seen(session, event_id, utc_now())
        finally:
            session.close()

        background_tasks.add_task(_process_in_background, session_factory, payload)
        return {"status": "ok"}

    return app


def _process_in_background(session_factory: sessionmaker, payload: dict) -> None:
    session = session_factory()
    try:
        process_webhook_event(session, payload, utc_now())
    finally:
        session.close()
