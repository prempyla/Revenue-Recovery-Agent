"""The one conversion point between a real UTC clock and the IST-anchored
business rules that actually govern this system: contact hours.

P1 timezone bug (2026-08-25, DECISIONS.md): full_agent.py's contact-hours
veto used to compare `action_time.hour` directly against a customer's
declared IST window. That's correct only by accident, when the process
happens to be running on an IST host. On any UTC-hosted deployment (the
only realistic one), the guardrail silently evaluated against the wrong
clock -- off by 5 hours 30 minutes, wide enough to flip an outside-hours
contact into an allowed one or vice versa.

funds_arrival_day / salary timing (ground_truth.py's `_insufficient_funds`)
does NOT need this: it's elapsed-duration arithmetic
(`payment.failed_at + timedelta(days=...)`, then `_hours_between(...)`),
not a wall-clock-hour-of-day read. A duration between two datetimes is the
same number of hours regardless of what zone either endpoint is expressed
in, as long as both are internally consistent -- confirmed by reading
ground_truth.py, not assumed. contact_hours is the only real "what hour of
the day is it, locally" concept in this codebase.

The simulator has always treated naive datetimes as IST wall-clock
directly (see harness.py's original note, kept true here) -- a closed,
self-consistent convention for a purely synthetic system that never reads
a real clock. Nothing about payment/persona generation, the Bernoulli
draws, or ground_truth.py's formulas changes because of this module.
What DOES need to handle both worlds is any check that reads an hour off a
datetime that might legitimately be either that naive IST convention, or a
timezone-aware value arriving from execution/ (a real UTC clock, since the
P1 fix -- see execution/clock.py). ist_hour() is that one conversion point,
used by both full_agent.py's outside_contact_hours veto and harness.py's
post-hoc invariant check, so this logic lives in exactly one place instead
of a bare `.hour` read scattered at each call site.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def ist_hour(dt: datetime) -> int:
    """The IST wall-clock hour to evaluate a contact_hours-style window
    against. A naive `dt` is assumed to already BE IST wall-clock (the
    simulator's own convention, unchanged); a timezone-aware `dt` (any
    zone, including execution/'s UTC) is converted to Asia/Kolkata first."""
    if dt.tzinfo is None:
        return dt.hour
    return dt.astimezone(IST).hour
