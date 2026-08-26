#!/usr/bin/env python3
"""Runs the kill -9 scenario for real. Not a design claim -- an actual
subprocess, actually SIGKILLed mid-dispatch, against a real file-backed
SQLite DB, then actually restarted and actually reconciled.

    5 payments -> full_agent decides -> orchestrator schedules ->
    worker_1 (a real subprocess) starts dispatching -> SIGKILLed while
    genuinely inside the Razorpay call for payment #1 -> worker_2 (a
    fresh subprocess) restarts -> reconciliation closes the gap for
    whatever's still stuck.

`now` is passed explicitly at every step (simulated time, matching this
codebase's no-clock-reads discipline throughout) so the demo doesn't
have to actually wait out a 1-hour due_at or a multi-minute lease in
real wall-clock time -- only the SIGKILL itself needs genuine OS-level
synchronization, via the ABOUT_TO_SLEEP marker worker_1 prints.

Usage: .venv/bin/python scripts/demo_crash_recovery.py
"""

import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent / "src"))

from _demo_common import DemoFileBackedRazorpayClient, print_ledger, print_outbox_snapshot  # noqa: E402
from execution.db import make_engine, make_session_factory  # noqa: E402
from execution.eventlog import derive_state  # noqa: E402
from execution.orchestrator import diagnose_and_schedule  # noqa: E402
from execution.reconciliation import run_reconciliation_poll_once  # noqa: E402
from execution.states import PaymentState  # noqa: E402
from simulator import ContactTracker  # noqa: E402
from simulator.full_agent import make_full_agent_policy  # noqa: E402
from simulator.types import Customer, DeclineReason, InstrumentType, Language, Payment  # noqa: E402

CHILD_SCRIPT = SCRIPT_DIR / "_crash_recovery_worker.py"
N_PAYMENTS = 5
LEASE_SECONDS = 180  # simulated
WORKER_LATENCY_SECONDS = 2.5  # REAL seconds worker_1 sleeps mid-dispatch -- the SIGKILL window
RECONCILIATION_STALENESS_MINUTES = 1

PERSONA = Customer(
    customer_id="cust_crash_demo",
    recovery_propensity=0.6,
    funds_arrival_day=2,
    contact_hours=(0, 24),
    annoyance_threshold=99,
    channel_response_rate={"upi_link": 0.7, "whatsapp": 0.6, "sms": 0.5},
    preferred_language=Language.EN,
)


def build_payments(failed_at: datetime):
    # INSUFFICIENT_FUNDS -> send_payment_link: the ONLY action_type with a
    # real dispatch (client.create_payment_link) behind it. Every other
    # action_type is a logged SIMULATED send that never calls the client
    # at all -- this scenario needs a real in-flight API call to crash
    # inside of, so this is the one category that can produce it.
    return [
        Payment(
            payment_id=f"pay_crash_{i:02d}",
            customer_id=PERSONA.customer_id,
            amount_paise=250_000 + i * 15_000,
            instrument_type=InstrumentType.CARD,
            decline_reason=DeclineReason.INSUFFICIENT_FUNDS,
            issuer_code="HDFC",
            failed_at=failed_at,
        )
        for i in range(N_PAYMENTS)
    ]


def run_worker_subprocess(*, db_path, fake_server_db, worker_id, worker_now, lease_seconds, latency_seconds, catch_marker):
    """catch_marker=True: stream stdout live and return as soon as the
    ABOUT_TO_SLEEP marker appears, WITHOUT waiting for the process to
    exit (the caller is responsible for killing/waiting on it).
    catch_marker=False: run to completion and return the finished
    process."""
    cmd = [
        sys.executable, str(CHILD_SCRIPT),
        "--db-path", db_path,
        "--fake-server-db-path", fake_server_db,
        "--worker-id", worker_id,
        "--worker-now", worker_now.isoformat(),
        "--lease-seconds", str(lease_seconds),
        "--latency-seconds", str(latency_seconds),
    ]
    if not catch_marker:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        for line in proc.stdout.splitlines():
            print(f"  [{worker_id}] {line}")
        if proc.returncode != 0:
            print(f"  [{worker_id}] STDERR:\n{proc.stderr}")
        return proc

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    marker_reference_id = None
    for line in proc.stdout:
        line = line.rstrip()
        print(f"  [{worker_id}] {line}")
        if line.startswith("ABOUT_TO_SLEEP:"):
            marker_reference_id = line.split(":", 1)[1]
            break
    return proc, marker_reference_id


def count_double_charges(fake_server_db: str):
    conn = sqlite3.connect(fake_server_db)
    try:
        rows = conn.execute(
            "SELECT reference_id, COUNT(*) c FROM fake_payment_links GROUP BY reference_id HAVING c > 1"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM fake_payment_links").fetchone()[0]
        return rows, total
    finally:
        conn.close()


def main() -> None:
    print("=" * 78)
    print("RAZORPAY REVENUE RECOVERY AGENT -- CRASH RECOVERY DEMO")
    print("Real subprocess. Real SIGKILL. Real file-backed SQLite. Nothing")
    print("in this script simulates the crash in-process -- worker_1 below")
    print("IS a separate OS process, killed from outside it.")
    print("=" * 78)

    tmp_dir = tempfile.mkdtemp(prefix="crash_demo_")
    db_path = os.path.join(tmp_dir, "execution.db")
    fake_server_db = os.path.join(tmp_dir, "fake_razorpay_server.db")
    print(f"\nWorking directory: {tmp_dir}")

    diagnosis_time = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    payments = build_payments(diagnosis_time)
    payment_ids = [p.payment_id for p in payments]

    engine = make_engine(f"sqlite:///{db_path}")
    session_factory = make_session_factory(engine)
    session = session_factory()

    tracker = ContactTracker()
    policy_fn = make_full_agent_policy(failure_log=[], outage_events=[])(tracker)

    due_at = None
    for payment in payments:
        intent = diagnose_and_schedule(session, payment, PERSONA, diagnosis_time, policy_fn=policy_fn)
        assert intent is not None, f"{payment.payment_id}: full_agent returned no action -- demo setup is wrong"
        assert intent.action_type == "send_payment_link", (
            f"{payment.payment_id}: expected send_payment_link, got {intent.action_type}"
        )
        due_at = intent.due_at
    session.close()

    print_ledger(session_factory(), payment_ids, "STEP 1 -- full_agent diagnosed and scheduled all 5 payments")

    print(f"\nSTEP 2 -- starting worker_1 (real subprocess), due_at={due_at.isoformat()}")
    print(f"          worker_1's dispatch call sleeps {WORKER_LATENCY_SECONDS}s AFTER the fake Razorpay")
    print(f"          server durably records the link but BEFORE returning -- that's the SIGKILL window.")
    proc, killed_reference_id = run_worker_subprocess(
        db_path=db_path, fake_server_db=fake_server_db, worker_id="worker_1",
        worker_now=due_at, lease_seconds=LEASE_SECONDS, latency_seconds=WORKER_LATENCY_SECONDS,
        catch_marker=True,
    )

    if killed_reference_id is None:
        proc.wait(timeout=10)
        print("\n!! worker_1 finished before the marker was ever seen -- demo TIMING needs adjusting")
        print("!! (not a system finding -- this means the crash window was missed, not that nothing crashed)")
        sys.exit(1)

    print(f"  >>> worker_1 is genuinely inside the API call for {killed_reference_id} right now. Sending SIGKILL.")
    kill_time = time.time()
    proc.send_signal(signal.SIGKILL)
    returncode = proc.wait(timeout=10)
    kill_confirmed_time = time.time()
    print(f"  worker_1 exited: returncode={returncode} (expect -9, i.e. killed by SIGKILL)")
    print(f"  time from signal sent to process confirmed dead: {kill_confirmed_time - kill_time:.3f}s")

    verify_session = session_factory()
    print_ledger(verify_session, payment_ids, "STEP 3 -- ledger immediately after the crash")
    print_outbox_snapshot(verify_session, "immediately after the crash")
    stuck_payment_id = None
    for pid in payment_ids:
        if derive_state(verify_session, pid) == PaymentState.EXECUTING:
            stuck_payment_id = pid
    print(f"\n  Payment stuck in EXECUTING: {stuck_payment_id!r}")
    verify_session.close()

    restart_now = due_at + timedelta(seconds=LEASE_SECONDS + 30)
    print(f"\nSTEP 4 -- restarting as worker_2 (simulated now = worker_1's claim + lease + 30s)")
    run_worker_subprocess(
        db_path=db_path, fake_server_db=fake_server_db, worker_id="worker_2",
        worker_now=restart_now, lease_seconds=LEASE_SECONDS, latency_seconds=0.0,
        catch_marker=False,
    )

    verify_session = session_factory()
    print_ledger(verify_session, payment_ids, "STEP 5 -- ledger after worker_2 (restarted worker) ran")
    print_outbox_snapshot(verify_session, "after worker_2")
    stuck_state_after_restart = derive_state(verify_session, stuck_payment_id) if stuck_payment_id else None
    print(f"\n  {stuck_payment_id!r} state after restart: {stuck_state_after_restart.value if stuck_state_after_restart else None}")
    verify_session.close()

    reconcile_now = restart_now + timedelta(minutes=RECONCILIATION_STALENESS_MINUTES + 1)
    print(f"\nSTEP 6 -- running the reconciliation poller (staleness={RECONCILIATION_STALENESS_MINUTES}min, simulated)")
    recon_client = DemoFileBackedRazorpayClient(fake_server_db)
    recon_session = session_factory()
    results = run_reconciliation_poll_once(
        recon_session, recon_client, reconcile_now, staleness_minutes=RECONCILIATION_STALENESS_MINUTES
    )
    if not results:
        print("  (no stale payments found -- unexpected if a crash occurred)")
    for r in results:
        new_state = r.new_state.value if r.new_state else "(unchanged)"
        print(f"  {r.payment_id}: {r.previous_state.value} -> {new_state}  remote_status={r.remote_status!r}  action={r.action}")
    recon_session.close()

    verify_session = session_factory()
    print_ledger(verify_session, payment_ids, "STEP 7 -- FINAL ledger, after reconciliation")
    final_states = {pid: derive_state(verify_session, pid) for pid in payment_ids}
    verify_session.close()

    dupes, total_links = count_double_charges(fake_server_db)

    print("\n" + "=" * 78)
    print("VERDICT")
    print("=" * 78)
    print(f"Payments in this run: {len(payments)}")
    print(f"Payment killed mid-dispatch: {stuck_payment_id}")
    print(f"Real Razorpay-side records created (fake server DB): {total_links}")
    print(f"Double-charges (same reference_id recorded twice): {len(dupes)}  {dupes if dupes else ''}")
    terminal_ok = {PaymentState.RECOVERED, PaymentState.AWAITING_CONFIRMATION, PaymentState.SIMULATED_SENT}
    lost = [pid for pid, s in final_states.items() if s not in terminal_ok]
    print(f"Payments with no forward progress after the full sequence: {len(lost)}  {lost if lost else '(none)'}")
    for pid, s in sorted(final_states.items()):
        print(f"  {pid}: {s.value if s else None}")
    print("=" * 78)


if __name__ == "__main__":
    main()
