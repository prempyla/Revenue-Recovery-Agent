#!/usr/bin/env python3
"""Seeded, deterministic-in-shape (real-clock-anchored, since this demo
specifically exercises a real inbound HTTP boundary — see below) walkthrough
of the inbound-reply path: a customer replies STOP -> classify_reply() ->
apply_reply_intent() -> a DURABLE CustomerOptOutEvent -> a fresh
query_layer.contact_tracker_for() reconstruction (a brand-new engine/session,
not the one that wrote the opt-out) -> full_agent.decide()'s explicit_opt_out
veto fires on the customer's NEXT payment.

Closes the gap external cold review Finding 2 found (2026-08-27, see
INCIDENTS.md): classify_reply/apply_reply_intent were built, adversarially
tested, and called from nowhere outside tests. This script drives them
through execution/reply_webhook.py's real POST /replies route (via
fastapi.testclient.TestClient — in-process ASGI, no real network socket,
same "no network calls" guarantee as every other make target) — not by
hand-constructing a ContactTracker the way a unit test does.

Real-clock-anchored, unlike demo_run.py's fixed FAILED_AT: the opt-out
timestamp is recorded by reply_webhook.py's own utc_now() call (the correct
place for a live system to read the clock — the request boundary, same
convention webhook.py already uses), so this script reads the real clock
between its own steps too, to stay on the same timeline rather than mixing
a synthetic historical failed_at against a genuinely-current opt-out event.

Usage: .venv/bin/python scripts/demo_reply.py
"""

import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from _demo_common import print_ledger  # noqa: E402
from execution.clock import utc_now  # noqa: E402
from execution.db import make_engine, make_session_factory  # noqa: E402
from execution.orchestrator import diagnose_and_schedule  # noqa: E402
from execution.query_layer import contact_tracker_for, recent_failures  # noqa: E402
from execution.reply_webhook import create_reply_app  # noqa: E402
from llm.client import FakeLLMClient  # noqa: E402
from simulator.full_agent import make_full_agent_policy  # noqa: E402
from simulator.types import Customer, DeclineReason, InstrumentType, Language, Payment  # noqa: E402

CONTACT_HOURS = (0, 24)  # wide open -- this demo is about the opt-out veto, not the hours veto

PERSONA = Customer(
    customer_id="cust_reply_demo",
    recovery_propensity=0.6,
    funds_arrival_day=2,
    contact_hours=CONTACT_HOURS,
    annoyance_threshold=99,
    channel_response_rate={"upi_link": 0.7, "whatsapp": 0.6, "sms": 0.5},
    preferred_language=Language.EN,
)


def main() -> None:
    print("=" * 78)
    print("RAZORPAY REVENUE RECOVERY AGENT -- INBOUND-REPLY / OPT-OUT DEMO")
    print("classify_reply -> apply_reply_intent -> durable opt-out -> veto on")
    print("the NEXT decide(), through the live POST /replies route and a fresh")
    print("query_layer reconstruction -- not a hand-built tracker in a test.")
    print("=" * 78)

    tmp_dir = tempfile.mkdtemp(prefix="demo_reply_")
    db_path = f"{tmp_dir}/execution.db"
    print(f"\nWorking directory: {tmp_dir}")

    engine = make_engine(f"sqlite:///{db_path}")
    session_factory = make_session_factory(engine)
    session = session_factory()

    first_failed_at = utc_now()
    first_payment = Payment(
        payment_id="pay_reply_demo_1", customer_id=PERSONA.customer_id, amount_paise=250_000,
        instrument_type=InstrumentType.CARD, decline_reason=DeclineReason.AFA_3DS_DROPOFF,
        issuer_code="HDFC", failed_at=first_failed_at,
    )

    print("\nSTEP 1 -- full_agent diagnoses the first payment (no opt-out yet)")
    tracker_before = contact_tracker_for(session, PERSONA.customer_id)
    policy_before = make_full_agent_policy(
        failure_log=recent_failures(session, first_failed_at), outage_events=[]
    )(tracker_before)
    intent_1 = diagnose_and_schedule(session, first_payment, PERSONA, first_failed_at, policy_fn=policy_before)
    if intent_1 is not None:
        print(f"  {first_payment.payment_id:<20} {first_payment.decline_reason.value:<25} -> {intent_1.action_type} (scheduled)")
    else:
        print(f"  {first_payment.payment_id:<20} {first_payment.decline_reason.value:<25} -> STOP (policy chose not to act)")

    print("\nSTEP 2 -- customer replies STOP, through the live POST /replies route")
    llm_client = FakeLLMClient()
    llm_client.classify_response = {"intent": "OPT_OUT", "promised_date": None}
    app = create_reply_app(session_factory, llm_client=llm_client)
    reply_client = TestClient(app)
    resp = reply_client.post(
        "/replies",
        json={
            "customer_id": PERSONA.customer_id,
            "payment_id": first_payment.payment_id,
            "reply_text": "please STOP contacting me",
            "contact_hours": list(CONTACT_HOURS),
        },
    )
    print(f"  POST /replies -> {resp.status_code} {resp.json()}")
    assert resp.status_code == 200 and resp.json()["intent"] == "OPT_OUT"

    print("\nSTEP 3 -- confirm the opt-out is durable: a BRAND NEW engine/session")
    print("          reconnecting to the same file, not the one that wrote it")
    engine_2 = make_engine(f"sqlite:///{db_path}")
    session_2 = make_session_factory(engine_2)()
    tracker_after = contact_tracker_for(session_2, PERSONA.customer_id)
    opted_out = tracker_after.is_opted_out(PERSONA.customer_id)
    print(f"  is_opted_out (fresh engine, reconstructed from disk): {opted_out}")
    assert opted_out is True

    second_failed_at = utc_now()  # strictly after the opt-out event recorded in STEP 2
    second_payment = Payment(
        payment_id="pay_reply_demo_2", customer_id=PERSONA.customer_id, amount_paise=800_000,
        instrument_type=InstrumentType.CARD, decline_reason=DeclineReason.INSUFFICIENT_FUNDS,
        issuer_code="HDFC", failed_at=second_failed_at,
    )
    print("\nSTEP 4 -- full_agent diagnoses a SECOND payment for the SAME customer,")
    print("          with a tracker sourced fresh from the live query layer")
    policy_after = make_full_agent_policy(
        failure_log=recent_failures(session_2, second_failed_at), outage_events=[]
    )(tracker_after)
    intent_2 = diagnose_and_schedule(session_2, second_payment, PERSONA, second_failed_at, policy_fn=policy_after)
    if intent_2 is None:
        print(f"  {second_payment.payment_id:<20} {second_payment.decline_reason.value:<25} -> STOP (vetoed: explicit_opt_out)")
    else:
        print(f"  UNEXPECTED: {second_payment.payment_id} was scheduled ({intent_2.action_type}) -- the veto did not fire")

    print_ledger(session_2, [first_payment.payment_id, second_payment.payment_id], "FINAL ledger")

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"  {first_payment.payment_id:<20} diagnosed and scheduled, before any opt-out")
    print("  customer replies STOP -> POST /replies -> classify_reply -> apply_reply_intent")
    print("  -> CustomerOptOutEvent persisted (confirmed surviving a fresh engine/session)")
    print(f"  {second_payment.payment_id:<20} diagnosed AFTER the opt-out -> vetoed by explicit_opt_out,")
    print("                       via query_layer.contact_tracker_for(), not a hand-built tracker")
    print("=" * 78)

    if intent_2 is not None:
        raise SystemExit("the explicit_opt_out veto did not fire through the live path -- see output above")


if __name__ == "__main__":
    main()
