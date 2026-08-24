"""Systemic outage detector — the "diagnose, don't just detect" piece.

A sliding-window count-threshold detector per issuer_code, with hysteresis
(stays flagged through a cooldown period after the count drops back under
threshold, instead of flapping on/off every time a single failure ages out
of the window).

Deliberately separate from the decision policy and NOT wired into
full_agent or the harness yet — a standalone pure function, same discipline
as ground_truth.success_probability() and the decide() function described in
DECISIONS.md: no I/O, no direct API calls, no internal clock reads, no
mutation. `now` and the observable failure_log are the only inputs.

Hysteresis without a stored .flagged flag: detect_systemic_event() stays
pure by reconstructing whether the issuer is still "in cooldown" from the
failure_log itself, the same trick used for Customer contact_count/opt-out
in contact_tracking.py — replay history from the arguments given, rather
than reading or mutating any state that outlives the call.

Only decline_reason == ISSUER_DOWN failures are observable to this detector
(a real system diagnoses from failures it can see; recovery_propensity,
funds_arrival_day etc. are hidden ground truth the detector never touches).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from .types import DeclineReason, Payment


@dataclass(frozen=True)
class OutageDetectorConfig:
    window_minutes: int
    count_threshold: int
    cooldown_minutes: int


def _issuer_down_times(failure_log: Sequence[Payment], issuer_code: str, now: datetime):
    return sorted(
        p.failed_at
        for p in failure_log
        if p.issuer_code == issuer_code
        and p.decline_reason == DeclineReason.ISSUER_DOWN
        and p.failed_at <= now
    )


def _count_in_trailing_window(times, end_time: datetime, window: timedelta) -> int:
    window_start = end_time - window
    return sum(1 for t in times if window_start < t <= end_time)


def detect_systemic_event(
    failure_log: Sequence[Payment],
    issuer_code: str,
    now: datetime,
    config: OutageDetectorConfig,
) -> bool:
    """True if issuer_code has a systemic event in progress at `now`.

    The sliding-window count can only increase at a failure's own arrival and
    decrease as failures age out of the trailing window — so its maxima all
    occur exactly at failure timestamps. Evaluating the trailing-window count
    at every observed failure time (up to and including `now`) is therefore
    enough to find every instant the threshold was breached, without needing
    to sample continuously. The most recent such breach determines whether
    `now` still falls inside its cooldown period.
    """
    window = timedelta(minutes=config.window_minutes)
    cooldown = timedelta(minutes=config.cooldown_minutes)
    times = _issuer_down_times(failure_log, issuer_code, now)
    if not times:
        return False

    last_breach_time = None
    for t in times:
        count = _count_in_trailing_window(times, t, window)
        if count > config.count_threshold:
            last_breach_time = t

    if last_breach_time is None:
        return False
    return (now - last_breach_time) < cooldown
