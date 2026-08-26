#!/usr/bin/env python3
"""Seeded, deterministic end-to-end walkthrough of the real execution
pipeline: full_agent decides -> orchestrator schedules -> the outbox
worker dispatches (real Razorpay-shaped calls via FakeRazorpayClient,
zero network) -> a webhook confirms the real one -> the ledger shows
every payment's final state.

Four payments, one per interesting category, all through the SAME
policy (full_agent) so the terminal output demonstrates taxonomy
routing, a real dispatch + webhook confirmation, a logged simulated
send, a policy-level abandon, and the jittered/ramped outage-hold path
-- in one short, legible run. Deterministic by construction (fixed
inputs, no randomness anywhere), so it prints the same thing every time.

Usage: .venv/bin/python scripts/demo_run.py
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent / "src"))

from _demo_common import print_ledger, print_outbox_snapshot  # noqa: E402
from execution.db import make_engine, make_session_factory  # noqa: E402
from execution.orchestrator import diagnose_and_schedule  # noqa: E402
from execution.outbox import run_outbox_worker_once  # noqa: E402
from execution.razorpay_client import FakeRazorpayClient  # noqa: E402
from execution.webhook import process_webhook_event  # noqa: E402
from simulator import ContactTracker  # noqa: E402
from simulator.full_agent import make_full_agent_policy  # noqa: E402
from simulator.types import Customer, DeclineReason, InstrumentType, Language, Payment  # noqa: E402

FAILED_AT = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)

PERSONA = Customer(
    customer_id="cust_demo",
    recovery_propensity=0.6,
    funds_arrival_day=2,
    contact_hours=(0, 24),
    annoyance_threshold=99,
    channel_response_rate={"upi_link": 0.7, "whatsapp": 0.6, "sms": 0.5},
    preferred_language=Language.EN,
)


def build_scenario():
    """4 payments, 4 distinct outcomes, all through full_agent."""
    real_dispatch = Payment(
        payment_id="pay_demo_funds", customer_id=PERSONA.customer_id, amount_paise=450_000,
        instrument_type=InstrumentType.CARD, decline_reason=DeclineReason.INSUFFICIENT_FUNDS,
        issuer_code="HDFC", failed_at=FAILED_AT,
    )
    simulated_dispatch = Payment(
        payment_id="pay_demo_timeout", customer_id=PERSONA.customer_id, amount_paise=180_000,
        instrument_type=InstrumentType.CARD, decline_reason=DeclineReason.NETWORK_TIMEOUT,
        issuer_code="ICICI", failed_at=FAILED_AT,
    )
    policy_stop = Payment(
        payment_id="pay_demo_mandate", customer_id=PERSONA.customer_id, amount_paise=90_000,
        instrument_type=InstrumentType.MANDATE, decline_reason=DeclineReason.MANDATE_REVOKED,
        issuer_code="SBI", failed_at=FAILED_AT,
    )
    outage_held = Payment(
        payment_id="pay_demo_outage", customer_id=PERSONA.customer_id, amount_paise=620_000,
        instrument_type=InstrumentType.CARD, decline_reason=DeclineReason.ISSUER_DOWN,
        issuer_code="AXIS", failed_at=FAILED_AT,
    )
    # A burst of OTHER customers' failures against the same issuer, dense
    # enough to trip detect_systemic_event at the near-term (+20min)
    # check under the PRODUCTION detector config (count_threshold=6,
    # strictly greater-than -- 7 failures needed, not the smaller bursts
    # used against the lower threshold in the circuit-breaker unit
    # tests) -- without this, pay_demo_outage would just retry in ~20min
    # like any other ISSUER_DOWN payment with no active outage, and the
    # jitter/circuit-breaker path this demo exists to show wouldn't fire.
    failure_log = [
        Payment(
            payment_id=f"pay_burst_{i}", customer_id="cust_other", amount_paise=100_000,
            instrument_type=InstrumentType.CARD, decline_reason=DeclineReason.ISSUER_DOWN,
            issuer_code="AXIS", failed_at=FAILED_AT + timedelta(minutes=m),
        )
        for i, m in enumerate([6, 8, 10, 12, 14, 16, 18])
    ]
    return [real_dispatch, simulated_dispatch, policy_stop, outage_held], failure_log


def main() -> None:
    print("=" * 78)
    print("RAZORPAY REVENUE RECOVERY AGENT -- END-TO-END DEMO")
    print("Real execution/ pipeline: full_agent -> orchestrator -> outbox worker")
    print("-> (a real one) webhook. FakeRazorpayClient throughout -- zero network.")
    print("=" * 78)

    payments, failure_log = build_scenario()
    payment_ids = [p.payment_id for p in payments]

    tmp_dir = tempfile.mkdtemp(prefix="demo_run_")
    db_path = f"{tmp_dir}/execution.db"
    print(f"\nWorking directory: {tmp_dir}")

    engine = make_engine(f"sqlite:///{db_path}")
    session_factory = make_session_factory(engine)
    session = session_factory()
    client = FakeRazorpayClient()

    tracker = ContactTracker()
    policy_fn = make_full_agent_policy(failure_log=failure_log, outage_events=[])(tracker)

    print("\nSTEP 1 -- full_agent diagnoses each payment")
    intents = {}
    for payment in payments:
        intent = diagnose_and_schedule(session, payment, PERSONA, FAILED_AT, policy_fn=policy_fn)
        if intent is not None:
            offset = intent.due_at - FAILED_AT
            offset_display = str(timedelta(seconds=round(offset.total_seconds())))
            print(f"  {payment.payment_id:<20} {payment.decline_reason.value:<25} -> {intent.action_type:<28} due_at=+{offset_display}")
            intents[payment.payment_id] = intent
        else:
            print(f"  {payment.payment_id:<20} {payment.decline_reason.value:<25} -> STOP (policy chose not to act)")

    print_ledger(session_factory(), payment_ids, "STEP 2 -- ledger after diagnosis (before any dispatch)")

    print("\nSTEP 3 -- the outbox worker runs, polling at each payment's own due_at")
    latest_due = max(i.due_at for i in intents.values())
    processed = run_outbox_worker_once(session, client, latest_due, "demo_worker", lease_duration=timedelta(minutes=10))
    for intent in processed:
        print(f"  dispatched: {intent.payment_id:<20} action={intent.action_type:<28} result={'simulated' if intent.result.get('simulated') else 'REAL call'}")

    print_ledger(session_factory(), payment_ids, "STEP 4 -- ledger after the outbox worker dispatches")
    print_outbox_snapshot(session_factory(), "after dispatch")

    print("\nSTEP 5 -- a real webhook confirms the one real dispatch (pay_demo_funds)")
    dispatched_link_id = next(intent.result["id"] for intent in processed if intent.payment_id == "pay_demo_funds")
    real_intent = intents["pay_demo_funds"]
    webhook_payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {"reference_id": real_intent.idempotency_key, "id": dispatched_link_id}
            }
        },
    }
    acted_on = process_webhook_event(session, webhook_payload, latest_due)
    print(f"  webhook processed for: {acted_on}")

    print_ledger(session_factory(), payment_ids, "STEP 6 -- FINAL ledger")

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"  {'pay_demo_funds':<20} INSUFFICIENT_FUNDS -> real send_payment_link -> webhook -> RECOVERED")
    print(f"  {'pay_demo_timeout':<20} NETWORK_TIMEOUT     -> retry_now, no real primitive -> SIMULATED_SENT")
    print(f"  {'pay_demo_mandate':<20} MANDATE_REVOKED     -> outside auto-retry scope -> ABANDONED(policy_stop)")
    print(f"  {'pay_demo_outage':<20} ISSUER_DOWN         -> held for a detected outage, jittered release -> SIMULATED_SENT")
    print("=" * 78)


if __name__ == "__main__":
    main()
