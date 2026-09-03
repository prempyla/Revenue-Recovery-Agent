"""Inbound customer-reply receiver — the live path Finding 2 (external cold
review, 2026-08-27, see INCIDENTS.md) found missing entirely. llm.classify_
reply() and llm.reply_handling.apply_reply_intent() were built, adversarially
tested (tests/test_llm_classification.py, tests/test_llm_reply_handling.py),
and called from NOWHERE outside tests — no script, route, or scheduler ever
fed a real customer reply into them. Concretely: explicit_opt_out, which
DECISIONS.md itself calls "the most serious compliance failure available to
this system," could not fire in any live run, because nothing durable ever
got written for classify_reply/apply_reply_intent to act on outside a test's
own hand-built ContactTracker.

Mirrors webhook.py's shape on purpose: a thin FastAPI POST endpoint, fast ack,
real persistence via the same session/db every other execution/ component
uses — not a parallel, test-only code path. Unlike webhook.py there is no
real inbound provider on the other side of this in this repo (a genuine
deployment would sit this behind a WhatsApp/SMS Business API webhook —
Twilio, Gupshup, etc. — out of scope here, the same way razorpay_client.py's
RealAnthropicClient/RealRazorpayClient are exercised only via manual
verification, never automated tests, per DECISIONS.md).

Known interface gap, reported rather than hidden — same class as
query_layer.py's outage_events (documented "nothing to query for it") and
orchestrator.py's policy_fn pre-binding: apply_reply_intent needs a full
simulator.types.Customer persona (contact_hours, annoyance_threshold, etc.),
and nothing under execution/ persists or reconstructs one — Customer is
simulator-side ground truth with no execution/-side analog; a real
deployment would source it from a merchant's actual customer-profile store,
which doesn't exist here. Of Customer's fields, only contact_hours is
actually READ by decide()'s veto path for this flow (annoyance_threshold is
hidden ground truth the policy layer never sees at all, even in the
simulator) — the rest are required by the dataclass shape with no functional
effect here, so the request only asks for contact_hours and defaults the
others to inert placeholders, documented below, not silently invented as if
they were real.
"""

from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from llm.classification import classify_reply
from llm.client import LLMClientInterface
from llm.reply_handling import apply_reply_intent
from simulator.types import Customer, Language

from .clock import utc_now
from .query_layer import contact_tracker_for, payment_by_id, recent_failures


class ReplyRequest(BaseModel):
    customer_id: str
    payment_id: str
    reply_text: str
    # The one Customer field the live veto path actually reads. See module
    # docstring: everything else on Customer is a simulator-only ground-truth
    # field with no execution/-side source, defaulted below, not invented.
    contact_hours: Tuple[int, int] = (0, 24)


def _placeholder_customer(customer_id: str, contact_hours: Tuple[int, int]) -> Customer:
    """The fields below are never read on this path (see module docstring)
    — placeholders, not a real profile. Only contact_hours is real input."""
    return Customer(
        customer_id=customer_id,
        recovery_propensity=0.0,
        funds_arrival_day=0,
        contact_hours=contact_hours,
        annoyance_threshold=0,
        channel_response_rate={},
        preferred_language=Language.EN,
    )


def handle_reply(
    session: Session,
    customer_id: str,
    payment_id: str,
    reply_text: str,
    now: datetime,
    llm_client: Optional[LLMClientInterface] = None,
    contact_hours: Tuple[int, int] = (0, 24),
) -> Dict[str, Any]:
    """The actual logic, `now` explicit and never read from the clock here
    — same split as webhook.py's process_webhook_event vs. its route: this
    function is what tests inject an exact `now` against (so an opt-out
    written here and a later decide() call can be placed at deterministic,
    comparable times), and create_reply_app's route below is the one place
    that reads the real clock, at the request boundary, same as webhook.py
    already does for mark_event_seen/process_webhook_event."""
    classified = classify_reply(reply_text, llm_client)

    payment = payment_by_id(session, payment_id)
    if payment is None:
        # Same honesty as query_layer's own skip-don't-fabricate rule: no
        # DIAGNOSED payment under this id, so there's nothing to hang
        # source_payment_id on. Still record the classification outcome;
        # still safe to return.
        return {"intent": classified.intent.value, "outcome": None, "payment_found": False}

    customer = _placeholder_customer(customer_id, contact_hours)
    tracker = contact_tracker_for(session, customer_id)
    failure_log = recent_failures(session, now)

    outcome = apply_reply_intent(session, classified, payment, customer, tracker, failure_log, [], now)
    return {
        "intent": classified.intent.value,
        "outcome": None
        if outcome is None
        else {"offset_seconds": outcome[0].total_seconds(), "action_type": outcome[1].action_type.value},
        "payment_found": True,
    }


def create_reply_app(session_factory: sessionmaker, llm_client: Optional[LLMClientInterface] = None) -> FastAPI:
    app = FastAPI()

    @app.post("/replies")
    async def receive_reply(body: ReplyRequest):
        session = session_factory()
        try:
            return handle_reply(
                session,
                body.customer_id,
                body.payment_id,
                body.reply_text,
                utc_now(),
                llm_client=llm_client,
                contact_hours=body.contact_hours,
            )
        finally:
            session.close()

    return app
