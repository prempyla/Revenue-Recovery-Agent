"""Shared infrastructure for the demo scripts: a crash-durable fake
Razorpay client, and a ledger printer readable at video resolution.

Not imported by anything under src/ -- this is demo-only scaffolding, kept
out of the system being demonstrated.
"""

import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from execution.eventlog import history  # noqa: E402
from execution.outbox import OutboxIntent  # noqa: E402
from execution.razorpay_client import PaymentLinkResult, RazorpayClientInterface  # noqa: E402


class DemoFileBackedRazorpayClient(RazorpayClientInterface):
    """A fake Razorpay whose server-side records survive OUR process dying.

    execution.razorpay_client.FakeRazorpayClient is deliberately in-memory
    only -- correct for every existing test, all of which run in a single
    process. It is the WRONG fake for this demo specifically: if the
    worker process gets SIGKILLed, an in-memory fake dies with it, so a
    restarted process could never observe that the call had actually
    landed before the crash -- making it impossible to honestly exercise
    reconciliation's "remote record exists, catch up" path. Real
    Razorpay's servers don't die when our worker does; this client's
    backing SQLite file is what stands in for that fact here.

    An optional `pre_return_latency_seconds` sleeps AFTER the record is
    durably written but BEFORE returning to the caller -- simulating the
    exact crash window outbox.py's own docstring describes: a process
    that dies after the real work happened but before it got to record
    the result locally.
    """

    def __init__(
        self, db_path: str, pre_return_latency_seconds: float = 0.0, pre_write_latency_seconds: float = 0.0
    ):
        self._db_path = db_path
        self._latency = pre_return_latency_seconds
        self._pre_write_latency = pre_write_latency_seconds
        conn = sqlite3.connect(db_path)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS fake_payment_links (
                reference_id TEXT PRIMARY KEY,
                id TEXT NOT NULL,
                short_url TEXT NOT NULL,
                status TEXT NOT NULL
            )"""
        )
        conn.commit()
        conn.close()

    def create_payment_link(
        self,
        *,
        amount_paise: int,
        customer_name: str,
        customer_contact: str,
        reference_id: str,
        description: str = "",
    ) -> PaymentLinkResult:
        if self._pre_write_latency > 0:
            # The OTHER crash window: killed before the call even reaches
            # "the server" -- no remote record will ever exist for this
            # reference_id. Exercises reconciliation's other branch
            # (remote is None -> ABANDONED(execution_error)), distinct
            # from the "record exists, catch up" branch the default
            # pre-return latency exercises.
            print(f"ABOUT_TO_SLEEP_PRE_WRITE:{reference_id}", flush=True)
            time.sleep(self._pre_write_latency)

        conn = sqlite3.connect(self._db_path)
        try:
            existing = conn.execute(
                "SELECT 1 FROM fake_payment_links WHERE reference_id = ?", (reference_id,)
            ).fetchone()
            if existing:
                raise RuntimeError(f"reference_id already exists: {reference_id}")

            link_id = f"plink_demo_{reference_id.replace(':', '_')}"
            short_url = f"https://fake.razorpay.link/{link_id}"
            # The "real Razorpay server" durably records the link HERE --
            # committed before this function does anything else, exactly
            # like a real API call either lands server-side or it doesn't,
            # independent of whether OUR process is still alive a moment
            # later to hear about it.
            conn.execute(
                "INSERT INTO fake_payment_links (reference_id, id, short_url, status) VALUES (?, ?, ?, ?)",
                (reference_id, link_id, short_url, "created"),
            )
            conn.commit()
        finally:
            conn.close()

        if self._latency > 0:
            # Synchronization point for the parent process: printed only
            # AFTER the "server-side" record above is durably committed,
            # so a kill landing during this sleep leaves a real record
            # behind for reconciliation to find -- not a race against our
            # own write.
            print(f"ABOUT_TO_SLEEP:{reference_id}", flush=True)
            time.sleep(self._latency)

        return {"id": link_id, "short_url": short_url, "status": "created", "reference_id": reference_id}

    def fetch_payment_link_status(self, reference_id: str) -> Optional[PaymentLinkResult]:
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT id, short_url, status FROM fake_payment_links WHERE reference_id = ?",
                (reference_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        link_id, short_url, status = row
        return {"id": link_id, "short_url": short_url, "status": status, "reference_id": reference_id}


def utc_now_display(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%H:%M:%S.%f")[:-3] + "Z"


_STATE_WIDTH = 20
_ID_WIDTH = 20
_TIME_WIDTH = 13


def print_ledger(session, payment_ids, title: str) -> None:
    """A fixed-width, aligned table of every payment's current derived
    state and the reason attached to its most recent event -- legible at
    video resolution: no ANSI color dependency, wide columns, one row per
    payment, sorted for a stable read order across repeated prints."""
    from execution.eventlog import derive_state

    print()
    print("=" * 78)
    print(title)
    print("=" * 78)
    header = f"{'PAYMENT_ID':<{_ID_WIDTH}} {'STATE':<{_STATE_WIDTH}} {'LAST EVENT':<{_TIME_WIDTH}} REASON / DETAIL"
    print(header)
    print("-" * len(header))
    for payment_id in sorted(payment_ids):
        events = history(session, payment_id)
        state = derive_state(session, payment_id)
        state_label = state.value if state else "(none)"
        if not events:
            print(f"{payment_id:<{_ID_WIDTH}} {'(no events)':<{_STATE_WIDTH}}")
            continue
        last = events[-1]
        last_time = utc_now_display(last.event_time)
        detail = ""
        if last.abandon_reason:
            detail = f"abandon_reason={last.abandon_reason}"
        elif last.payload:
            source = last.payload.get("source")
            simulated = last.payload.get("simulated")
            bits = [b for b in (f"source={source}" if source else None, "simulated=True" if simulated else None) if b]
            detail = " ".join(bits)
        print(f"{payment_id:<{_ID_WIDTH}} {state_label:<{_STATE_WIDTH}} {last_time:<{_TIME_WIDTH}} {detail}")
    print("=" * 78)


def print_outbox_snapshot(session, title: str) -> None:
    """The outbox_intents table's own view -- status/claimed_by/lease --
    shown separately from the payment ledger above because it answers a
    different question: not "what happened to this payment" but "who
    currently owns the intent to act on it, and until when.\""""
    print()
    print(f"--- outbox_intents: {title} ---")
    header = f"{'PAYMENT_ID':<{_ID_WIDTH}} {'STATUS':<10} {'CLAIMED_BY':<16} CLAIMED_UNTIL"
    print(header)
    print("-" * len(header))
    intents = session.query(OutboxIntent).order_by(OutboxIntent.payment_id).all()
    for intent in intents:
        claimed_until = utc_now_display(intent.claimed_until) if intent.claimed_until else "-"
        claimed_by = intent.claimed_by or "-"
        print(f"{intent.payment_id:<{_ID_WIDTH}} {intent.status:<10} {claimed_by:<16} {claimed_until}")
