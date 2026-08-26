"""The worker process spawned (and, in the crash scenario, SIGKILLed) by
scripts/demo_crash_recovery.py. Runs exactly one real
execution.outbox.run_outbox_worker_once pass against a file-backed
SQLite DB shared with the parent process, then exits.

Not a fake process pretending to crash -- this file IS the worker; the
parent sends it a real SIGKILL from outside while it's genuinely inside
a Python call. `now` is passed in explicitly (simulated time, matching
this whole codebase's no-clock-reads discipline), not read from the real
clock, so the demo doesn't need to actually wait out a 1-hour due_at or a
multi-minute lease in real time.
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _demo_common import DemoFileBackedRazorpayClient  # noqa: E402
from execution.db import make_engine, make_session_factory  # noqa: E402
from execution.outbox import run_outbox_worker_once  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--fake-server-db-path", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--worker-now", required=True, help="ISO datetime, simulated 'now' for this pass")
    parser.add_argument("--lease-seconds", type=float, default=180.0)
    parser.add_argument("--latency-seconds", type=float, default=0.0)
    parser.add_argument("--pre-write-latency-seconds", type=float, default=0.0)
    args = parser.parse_args()

    now = datetime.fromisoformat(args.worker_now)
    engine = make_engine(f"sqlite:///{args.db_path}")
    session = make_session_factory(engine)()
    client = DemoFileBackedRazorpayClient(
        args.fake_server_db_path,
        pre_return_latency_seconds=args.latency_seconds,
        pre_write_latency_seconds=args.pre_write_latency_seconds,
    )

    print(f"WORKER_START worker_id={args.worker_id} now={now.isoformat()}", flush=True)
    processed = run_outbox_worker_once(
        session, client, now, args.worker_id, lease_duration=timedelta(seconds=args.lease_seconds)
    )
    print(f"WORKER_DONE worker_id={args.worker_id} processed={len(processed)}", flush=True)
    for intent in processed:
        print(f"  processed: {intent.payment_id} status={intent.status}", flush=True)


if __name__ == "__main__":
    main()
