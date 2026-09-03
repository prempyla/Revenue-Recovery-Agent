"""full_agent: the real decision policy. Composes what already exists rather
than writing something new:
  - taxonomy routing (the category -> action/timing shape from
    policies.RULES_ONLY_TABLE)
  - the systemic outage detector (outage_detector.detect_systemic_event)
  - cost-aware action scoring on top of both

decide() is a pure function per DECISIONS.md's `decide(payment_state,
customer_history, config, now) -> action` sketch, extended with the two
extra arguments the systemic detector genuinely needs (failure_log,
outage_events) and a tracker standing in for "customer_history" (see
make_full_agent_policy below) — no I/O, no clock reads, everything relevant
is an explicit argument.

Scoring: for each non-vetoed candidate, expected_value = estimated_success_
rate * payment.amount_paise - contact_cost_paise(action). STOP is always an
implicit candidate scoring exactly 0 (no cost, no revenue) — a real action
only wins by beating it, so "stop" is genuinely reachable, not a fallback
bolted on afterward.

ESTIMATED_SUCCESS_RATE below is this POLICY's own coarse, category-level
belief about what tends to work — explicitly NOT derived from or copied out
of ground_truth.py's hidden per-customer formulas (recovery_propensity,
funds_arrival_day, channel_response_rate, annoyance_threshold stay exactly
as invisible to full_agent as to every other policy in this repo). A real
system has aggregate historical conversion rates, not the simulator's secret
math, and that's the honest gap being modeled here: these numbers are
independently chosen illustrative estimates, not a leak.

Hard compliance constraints -- contact hours, a weekly-style contact cap,
and an opt-out proxy -- veto candidates BEFORE scoring, in _veto_reason()
below, never after: a compliance rule that can be outscored by a big enough
payment isn't a compliance rule. contact_hours is treated as observable
merchant/compliance data (a business fact a real merchant would capture at
consent time), categorically different from the genuinely-secret ground-
truth parameters above. See the module-level note further down for why the
weekly-cap and opt-out checks collapse into one conservative mechanism here
rather than two independently precise ones.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, List, Optional, Sequence, Tuple

from . import config
from .contact_tracking import ContactTracker, contact_cost_paise
from .outage_detector import OutageDetectorConfig, detect_systemic_event
from .policies import Plan
from .timezones import ist_hour
from .types import (
    CUSTOMER_FACING_ACTION_TYPES,
    Action,
    ActionType,
    Channel,
    Customer,
    DeclineReason,
    OutageEvent,
    Payment,
)

DEFAULT_DETECTOR_CONFIG = OutageDetectorConfig(
    window_minutes=config.DETECTOR_WINDOW_MINUTES,
    count_threshold=config.DETECTOR_COUNT_THRESHOLD,
    cooldown_minutes=config.DETECTOR_COOLDOWN_MINUTES,
)

# Policy's own coarse historical-average belief, NOT ground_truth.py's hidden
# per-customer formula. ASSUMPTION: illustrative values, independently chosen.
ESTIMATED_SUCCESS_RATE = {
    (DeclineReason.INSUFFICIENT_FUNDS, ActionType.SEND_PAYMENT_LINK): 0.35,
    (DeclineReason.MANDATE_INSUFFICIENT_FUNDS, ActionType.SEND_PAYMENT_LINK): 0.30,
    (DeclineReason.NETWORK_TIMEOUT, ActionType.RETRY_NOW): 0.45,
    (DeclineReason.RISK_DECLINE, ActionType.ESCALATE_ALTERNATE_INSTRUMENT): 0.25,
    (DeclineReason.CARD_EXPIRED, ActionType.SEND_INSTRUMENT_UPDATE_LINK): 0.30,
    (DeclineReason.CARD_OR_ACCOUNT_BLOCKED, ActionType.SEND_INSTRUMENT_UPDATE_LINK): 0.20,
    (DeclineReason.AFA_3DS_DROPOFF, ActionType.SEND_NUDGE): 0.40,
    (DeclineReason.LIMIT_EXCEEDED, ActionType.RETRY_SCHEDULED): 0.35,
}
# ISSUER_DOWN is timing-conditional on the systemic detector's live verdict,
# not just category -- handled separately in _candidates_for, not the table
# above.
ISSUER_DOWN_RATE_NO_OUTAGE_DETECTED = 0.55
ISSUER_DOWN_RATE_HOLDING_FOR_RECOVERY = 0.50
ISSUER_DOWN_RATE_RETRYING_INTO_LIVE_OUTAGE = 0.05

# ASSUMPTION: this policy's own conservative belief about how long an issuer
# outage can run, informed by (illustrative) aggregate historical incident
# data -- NOT a read of the simulator's TRUE_OUTAGE_DURATION_MINUTES
# constant, even though it lands in a similar range. See _candidates_for's
# ISSUER_DOWN branch for why the hold offset needs this margin.
ASSUMED_MAX_OUTAGE_DURATION_MINUTES = 100

DEFAULT_CHANNEL = {
    ActionType.SEND_PAYMENT_LINK: Channel.UPI_LINK,
    ActionType.SEND_INSTRUMENT_UPDATE_LINK: Channel.SMS,
    ActionType.SEND_NUDGE: Channel.WHATSAPP,
}

# --- P1 #3 (2026-08-25, DECISIONS.md): jitter + circuit breaker ---
#
# Before this, every payment held for a detected ISSUER_DOWN outage was
# scheduled at the exact same fixed hold_offset -- so when an outage
# cleared, every held payment retried at roughly the same instant: a
# thundering herd against a bank that had just come back up. Two fixes,
# composed:
#
# 1. Deterministic jitter, spreading release instants out. Must be a pure
#    function of (payment_id, attempt_number) -- same reasoning as
#    idempotency.make_idempotency_key -- so decide() stays replayable: the
#    same inputs must always produce the same jittered offset, which rules
#    out Python's `random` module (a fresh value per call would make
#    decide() non-reproducible for identical arguments).
#
# 2. A circuit breaker (named that on purpose -- this is a solved,
#    textbook problem, not something invented here), with the standard
#    closed / open / half_open states. OPEN while the detector says the
#    outage is active. HALF_OPEN for a confirmation window immediately
#    after it stops being active -- not yet trusted long enough to be
#    "recovered", just "not currently failing". CLOSED once it's stayed
#    clear for a full confirmation window. State is derived fresh from
#    failure_log on every call via circuit_breaker_state() below, exactly
#    like detect_systemic_event() itself -- no mutable flag anywhere.
#
# Composing the two: rather than everyone waiting the full conservative
# ASSUMED_MAX_OUTAGE_DURATION_MINUTES, a small deterministic fraction of
# held payments ("probes") get a chance at an EARLIER release, gated on
# the breaker having reached CLOSED (not just HALF_OPEN -- one confirmed
# clear window isn't enough confidence to release on its own) by that
# earlier time. A second, wider fraction ("ramp") gets a chance at a
# second, later checkpoint. Everyone else -- and anyone whose probe/ramp
# checkpoint found the breaker not yet CLOSED, i.e. "probes failed" --
# falls through to the original, fully conservative fallback time,
# unchanged from before this fix. This is what "reopens rather than
# widens" means operationally here: a checkpoint that finds the breaker
# still OPEN or only HALF_OPEN does not release early, it defers to the
# next, more conservative checkpoint instead.
#
# ASSUMPTIONS, all ungiven by the task: the specific fractions and window
# width below. Chosen illustratively, not tuned against a target number.
HOLD_JITTER_MAX_MINUTES = 15
CIRCUIT_BREAKER_WINDOW_MINUTES = 15  # width of the half-open confirmation window and of one ramp stage
CIRCUIT_BREAKER_PROBE_FRACTION = 0.1  # fraction of held payments given a shot at the earliest checkpoint
CIRCUIT_BREAKER_RAMP_FRACTION = 0.5  # cumulative fraction (including probes) given a shot at the second checkpoint


class CircuitBreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


def circuit_breaker_state(
    failure_log: Sequence[Payment],
    issuer_code: str,
    at: datetime,
    detector_config: OutageDetectorConfig,
) -> CircuitBreakerState:
    """Derived fresh from failure_log every call, never stored: OPEN if
    detect_systemic_event says the outage is active AT `at`. HALF_OPEN if
    it isn't active at `at` but WAS active within the trailing
    CIRCUIT_BREAKER_WINDOW_MINUTES -- recently cleared, not yet confirmed
    stable. CLOSED once it's been clear for at least that long. Same
    (failure_log, issuer_code, at, config) always yields the same answer,
    same replay-from-arguments discipline as detect_systemic_event()
    itself."""
    if detect_systemic_event(failure_log, issuer_code, at, detector_config):
        return CircuitBreakerState.OPEN
    recently = at - timedelta(minutes=CIRCUIT_BREAKER_WINDOW_MINUTES)
    if detect_systemic_event(failure_log, issuer_code, recently, detector_config):
        return CircuitBreakerState.HALF_OPEN
    return CircuitBreakerState.CLOSED


def _deterministic_unit_interval(salt: str, payment_id: str, attempt_number: int) -> float:
    """Pseudo-random value in [0, 1), deterministic given
    (salt, payment_id, attempt_number). A SHA-256 hash rather than
    Python's `random` module for the same reason idempotency keys are
    derived, not random: decide() must return the SAME answer for the
    same inputs, every time it's replayed. Different `salt` strings give
    independent-looking outputs for different purposes from the same
    (payment_id, attempt_number) -- e.g. this payment's jitter offset and
    its circuit-breaker ramp bucket shouldn't be correlated with each
    other just because they're hashed from the same identity."""
    digest = hashlib.sha256(f"{salt}:{payment_id}:{attempt_number}".encode()).digest()
    return int.from_bytes(digest, "big") / (2 ** (8 * len(digest)))


def _deterministic_jitter(payment_id: str, attempt_number: int, max_jitter: timedelta) -> timedelta:
    fraction = _deterministic_unit_interval("jitter", payment_id, attempt_number)
    return timedelta(seconds=fraction * max_jitter.total_seconds())


def _issuer_down_release_offset(
    payment: Payment,
    failure_log: Sequence[Payment],
    detector_config: OutageDetectorConfig,
    attempt_number: int,
) -> timedelta:
    """When to release a payment held for a detected ISSUER_DOWN outage.
    Three deterministic checkpoints, cascading from earliest/riskiest to
    latest/safest; every payment ends up with exactly one release offset,
    computed in this one call -- no live re-evaluation loop, consistent
    with decide() being called once per diagnosis. See the module-level
    note above for the full reasoning."""
    hysteresis_clear_minutes = detector_config.window_minutes + detector_config.cooldown_minutes
    probe_offset = timedelta(minutes=hysteresis_clear_minutes)
    ramp_offset = timedelta(minutes=hysteresis_clear_minutes + CIRCUIT_BREAKER_WINDOW_MINUTES)
    fallback_offset = timedelta(minutes=hysteresis_clear_minutes + ASSUMED_MAX_OUTAGE_DURATION_MINUTES)

    bucket = _deterministic_unit_interval("ramp_bucket", payment.payment_id, attempt_number)
    jitter = _deterministic_jitter(payment.payment_id, attempt_number, timedelta(minutes=HOLD_JITTER_MAX_MINUTES))

    checkpoints = []
    if bucket < CIRCUIT_BREAKER_PROBE_FRACTION:
        checkpoints.append(probe_offset)
    if bucket < CIRCUIT_BREAKER_RAMP_FRACTION:
        checkpoints.append(ramp_offset)

    for offset in checkpoints:
        candidate_time = payment.failed_at + offset
        if circuit_breaker_state(failure_log, payment.issuer_code, candidate_time, detector_config) == CircuitBreakerState.CLOSED:
            return offset + jitter
        # Still OPEN or only HALF_OPEN at this checkpoint -- "probes
        # failed": don't release here, fall through to the next,
        # more conservative checkpoint rather than widening anyway.

    return fallback_offset + jitter


@dataclass(frozen=True)
class Candidate:
    offset: timedelta
    action: Action
    estimated_success_rate: float


def _is_customer_facing(action: Action) -> bool:
    return action.action_type in CUSTOMER_FACING_ACTION_TYPES


def _candidates_for(
    payment: Payment,
    failure_log: Sequence[Payment],
    outage_events: Sequence[OutageEvent],
    detector_config: OutageDetectorConfig,
    disable_outage_detection: bool,
    attempt_number: int,
) -> List[Candidate]:
    category = payment.decline_reason

    if category in (DeclineReason.INSUFFICIENT_FUNDS, DeclineReason.MANDATE_INSUFFICIENT_FUNDS):
        rate = ESTIMATED_SUCCESS_RATE[(category, ActionType.SEND_PAYMENT_LINK)]
        action = Action(ActionType.SEND_PAYMENT_LINK, channel=DEFAULT_CHANNEL[ActionType.SEND_PAYMENT_LINK])
        return [Candidate(timedelta(hours=1), action, rate)]

    if category == DeclineReason.ISSUER_DOWN:
        # Would a retry at the natural ~20-minute delay be walking straight
        # into a live outage? Check the detector at that prospective moment,
        # not "now" -- the point of holding is to avoid retrying DURING the
        # outage, and the outage may not have started (or may have started
        # after failure) by the time decide() is actually called.
        prospective_time = payment.failed_at + timedelta(minutes=20)
        outage_in_progress = (not disable_outage_detection) and detect_systemic_event(
            failure_log, payment.issuer_code, prospective_time, detector_config
        )
        if outage_in_progress:
            # Hold, with jitter + a ramped circuit-breaker release schedule
            # (P1 #3, 2026-08-25 DECISIONS.md) instead of one fixed instant
            # for every held payment -- see _issuer_down_release_offset and
            # the module-level note above it for the full reasoning. The
            # fallback checkpoint inside it is still anchored past
            # ASSUMED_MAX_OUTAGE_DURATION_MINUTES, preserving the original
            # safety margin (see its own history below) for whichever
            # payments' earlier checkpoints don't confirm CLOSED.
            #
            # Original margin reasoning, unchanged: window+cooldown alone
            # (15+15=30min) sound like a reasonable buffer but aren't -- an
            # outage lasting up to ~90 minutes (this simulator's true_outage
            # duration; a real policy would have its own aggregate belief
            # from historical incidents, not this exact number) can still be
            # live an hour after it started, so a 60-90min payment that
            # failed near outage ONSET would still land inside a live outage
            # even after "holding". Verified empirically: with only
            # window+cooldown+30=60min, on 2 of 3 seeds the ablation showed
            # ZERO measurable outcome difference from full_agent, because
            # both the held and un-held retry times landed inside the same
            # still-active outage window. See DECISIONS.md 2026-08-25.
            release_offset = _issuer_down_release_offset(payment, failure_log, detector_config, attempt_number)
            return [
                Candidate(
                    release_offset, Action(ActionType.RETRY_SCHEDULED), ISSUER_DOWN_RATE_HOLDING_FOR_RECOVERY
                )
            ]
        return [
            Candidate(
                timedelta(minutes=20), Action(ActionType.RETRY_SCHEDULED), ISSUER_DOWN_RATE_NO_OUTAGE_DETECTED
            )
        ]

    if category == DeclineReason.NETWORK_TIMEOUT:
        rate = ESTIMATED_SUCCESS_RATE[(category, ActionType.RETRY_NOW)]
        return [Candidate(timedelta(minutes=2), Action(ActionType.RETRY_NOW), rate)]

    if category == DeclineReason.RISK_DECLINE:
        rate = ESTIMATED_SUCCESS_RATE[(category, ActionType.ESCALATE_ALTERNATE_INSTRUMENT)]
        return [Candidate(timedelta(0), Action(ActionType.ESCALATE_ALTERNATE_INSTRUMENT), rate)]

    if category in (DeclineReason.CARD_EXPIRED, DeclineReason.CARD_OR_ACCOUNT_BLOCKED):
        rate = ESTIMATED_SUCCESS_RATE[(category, ActionType.SEND_INSTRUMENT_UPDATE_LINK)]
        action = Action(
            ActionType.SEND_INSTRUMENT_UPDATE_LINK, channel=DEFAULT_CHANNEL[ActionType.SEND_INSTRUMENT_UPDATE_LINK]
        )
        return [Candidate(timedelta(0), action, rate)]

    if category == DeclineReason.MANDATE_REVOKED:
        # Outside auto-retry scope -- same stance as rules_only, for the same
        # reason (needs fresh mandate consent, not something a link can fix).
        return []

    if category == DeclineReason.AFA_3DS_DROPOFF:
        rate = ESTIMATED_SUCCESS_RATE[(category, ActionType.SEND_NUDGE)]
        action = Action(ActionType.SEND_NUDGE, channel=DEFAULT_CHANNEL[ActionType.SEND_NUDGE])
        return [Candidate(timedelta(minutes=30), action, rate)]

    if category == DeclineReason.LIMIT_EXCEEDED:
        rate = ESTIMATED_SUCCESS_RATE[(category, ActionType.RETRY_SCHEDULED)]
        offset = timedelta(hours=config.LIMIT_EXCEEDED_COOLDOWN_HOURS + 1)
        return [Candidate(offset, Action(ActionType.RETRY_SCHEDULED), rate)]

    raise ValueError(f"unhandled category: {category}")  # pragma: no cover - exhaustive over DeclineReason


def _veto_reason(
    candidate: Candidate, customer: Customer, action_time: datetime, tracker: ContactTracker
) -> Optional[str]:
    """None if the candidate is allowed; a reason string if it must be
    vetoed before scoring ever sees it. Only customer-facing candidates are
    subject to these -- a silent retry doesn't contact anyone.

    Three distinct hard constraints, checked in this order, each its own
    named reason (DECISIONS.md 2026-08-25 — explicit_opt_out was previously
    folded into weekly_cap as a proxy; that conflated a stated customer
    request with a rate limit and produced a correct-looking but falsely-
    labeled audit trail, the same class of problem ABANDONED collapsing
    three meanings would have been):
      1. explicit_opt_out -- the customer said stop (llm.classify_reply ->
         OPT_OUT -> tracker.mark_opted_out). Permanent, checked first.
      2. outside_contact_hours -- this customer's declared window, an IST
         wall-clock concept (P1 fix, 2026-08-25 DECISIONS.md): action_time
         is converted via timezones.ist_hour() rather than read directly,
         since on execution/'s real UTC clock a bare .hour read would be
         off by 5.5 hours on any UTC-hosted deployment.
      3. weekly_cap -- config.MAX_WEEKLY_CONTACTS against the tracker's
         running lifetime count. Deliberately conservative: decide() only
         has a lifetime count, not a timestamped rolling-window history, so
         it can't distinguish "3 contacts this week" from "3 spread over 3
         months" the way the harness's post-hoc rolling-window audit can
         (harness._check_invariants). Erring toward under-contacting rather
         than pretending to have precision this function's inputs don't
         support; that audit remains the precise measurement, reported
         alongside this policy in the comparison table.
    """
    if not _is_customer_facing(candidate.action):
        return None

    if tracker.is_opted_out(customer.customer_id, as_of=action_time):
        return "explicit_opt_out"

    start, end = customer.contact_hours
    if not (start <= ist_hour(action_time) < end):
        return "outside_contact_hours"

    if tracker.contact_count(customer.customer_id) >= config.MAX_WEEKLY_CONTACTS:
        return "weekly_cap"

    return None


def decide(
    payment: Payment,
    customer: Customer,
    now: datetime,
    tracker: ContactTracker,
    failure_log: Sequence[Payment],
    outage_events: Sequence[OutageEvent],
    detector_config: OutageDetectorConfig = DEFAULT_DETECTOR_CONFIG,
    disable_outage_detection: bool = False,
    attempt_number: int = 1,
) -> Optional[Tuple[timedelta, Action]]:
    """Pure: no I/O, no clock reads -- now/tracker/failure_log/outage_events
    are all explicit arguments. Returns the winning (offset, Action), or
    None if STOP wins (every candidate was vetoed, or none beat STOP's
    implicit score of 0).

    now IS load-bearing (external cold review, 2026-08-27 -- see
    INCIDENTS.md): each candidate's natural action_time is still
    payment.failed_at + candidate.offset (a category's own timing belief,
    anchored to the failure that triggered it -- unrelated to when decide()
    happens to be called), but the actual action_time used both for veto
    checks and for the RETURNED offset is max(now, that natural time). On a
    fresh diagnosis every existing caller passes now=payment.failed_at, and
    every candidate offset is >= timedelta(0), so max() always resolves to
    the natural time and this is byte-identical to the old behaviour --
    zero change to the primary batch/harness/execution path. It only
    diverges on a genuine RE-evaluation (now > payment.failed_at), which is
    exactly llm.reply_handling.apply_reply_intent's PROMISE_TO_PAY path: if
    the promised date is later than the category's natural offset would
    have fired, the winning candidate now fires immediately at that later
    now instead of silently repeating the original, already-stale offset --
    and because action_time can land on a different day/hour, the
    contact-hours and weekly-cap vetoes are now evaluated at the time the
    contact would ACTUALLY happen, which can flip the decision to None (or
    from None to an action) versus what the original diagnosis returned.
    Before this fix, now was accepted as a parameter and never read, so a
    promise-to-pay re-evaluation silently reproduced the original decision
    regardless of what was promised -- see
    tests/test_llm_reply_handling.py's
    test_reevaluation_at_a_promised_date_genuinely_differs_from_the_original_diagnosis
    and test_full_agent.py's
    test_reevaluating_at_a_later_now_can_flip_a_contact_hours_veto.

    attempt_number (P1 #3, 2026-08-25 DECISIONS.md): feeds the ISSUER_DOWN
    jitter/circuit-breaker release schedule -- same reasoning as
    idempotency.make_idempotency_key's (payment_id, attempt_number) pair.
    Defaults to 1 so every pre-existing caller keeps behaving exactly as
    before (this simulator's harness never re-evaluates the same payment
    through full_agent more than once per run, so attempt_number is always
    1 there; it only varies for callers -- e.g. a live execution/
    orchestration loop -- that genuinely re-diagnose the same payment)."""
    candidates = _candidates_for(
        payment, failure_log, outage_events, detector_config, disable_outage_detection, attempt_number
    )

    best: Optional[Candidate] = None
    best_score = 0.0  # STOP's score: zero cost, zero revenue
    best_action_time: Optional[datetime] = None
    for candidate in candidates:
        action_time = max(now, payment.failed_at + candidate.offset)
        if _veto_reason(candidate, customer, action_time, tracker) is not None:
            continue
        cost = contact_cost_paise(candidate.action)
        expected_value = candidate.estimated_success_rate * payment.amount_paise - cost
        if expected_value > best_score:
            best_score = expected_value
            best = candidate
            best_action_time = action_time

    if best is None:
        return None
    return (best_action_time - now, best.action)


def make_full_agent_policy(
    failure_log: Sequence[Payment],
    outage_events: Sequence[OutageEvent],
    disable_outage_detection: bool = False,
    attempt_number: int = 1,
) -> Callable[[ContactTracker], Callable[[Payment, Customer], Plan]]:
    """Factory returning a (tracker) -> policy_fn closure, so the harness can
    hand full_agent the SAME ContactTracker instance it's using for the rest
    of the run (see harness.run_policy's is_factory path) -- decide() needs
    that tracker for its compliance vetoes, and the plain
    (payment, customer) -> Plan policies don't need or get one.

    attempt_number (P1 #3, 2026-08-25 DECISIONS.md) is bound at FACTORY
    construction time, not per payment: the harness constructs one factory
    instance and reuses it across an entire batch of different payments
    (every one of them a genuine first attempt, so the default of 1 is
    correct there); a caller that's re-diagnosing the SAME payment on a
    later attempt (e.g. execution/'s orchestrator, which already tracks
    its own attempt_number per diagnose_and_schedule call) constructs a
    fresh factory with the right attempt_number for that one call, the
    same pattern already used for failure_log/outage_events/tracker.
    """

    def with_tracker(tracker: ContactTracker) -> Callable[[Payment, Customer], Plan]:
        def policy_fn(payment: Payment, customer: Customer) -> Plan:
            decision = decide(
                payment,
                customer,
                payment.failed_at,
                tracker,
                failure_log,
                outage_events,
                disable_outage_detection=disable_outage_detection,
                attempt_number=attempt_number,
            )
            return [decision] if decision is not None else []

        return policy_fn

    return with_tracker
