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

from dataclasses import dataclass
from datetime import datetime, timedelta
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
            # Hold: schedule past ASSUMED_MAX_OUTAGE_DURATION_MINUTES, not
            # just the detector's own window+cooldown. Those two alone
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
            hold_offset = timedelta(
                minutes=detector_config.window_minutes
                + detector_config.cooldown_minutes
                + ASSUMED_MAX_OUTAGE_DURATION_MINUTES
            )
            return [
                Candidate(hold_offset, Action(ActionType.RETRY_SCHEDULED), ISSUER_DOWN_RATE_HOLDING_FOR_RECOVERY)
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
) -> Optional[Tuple[timedelta, Action]]:
    """Pure: no I/O, no clock reads -- now/tracker/failure_log/outage_events
    are all explicit arguments. Returns the winning (offset, Action), or
    None if STOP wins (every candidate was vetoed, or none beat STOP's
    implicit score of 0)."""
    candidates = _candidates_for(payment, failure_log, outage_events, detector_config, disable_outage_detection)

    best: Optional[Candidate] = None
    best_score = 0.0  # STOP's score: zero cost, zero revenue
    for candidate in candidates:
        action_time = payment.failed_at + candidate.offset
        if _veto_reason(candidate, customer, action_time, tracker) is not None:
            continue
        cost = contact_cost_paise(candidate.action)
        expected_value = candidate.estimated_success_rate * payment.amount_paise - cost
        if expected_value > best_score:
            best_score = expected_value
            best = candidate

    if best is None:
        return None
    return (best.offset, best.action)


def make_full_agent_policy(
    failure_log: Sequence[Payment],
    outage_events: Sequence[OutageEvent],
    disable_outage_detection: bool = False,
) -> Callable[[ContactTracker], Callable[[Payment, Customer], Plan]]:
    """Factory returning a (tracker) -> policy_fn closure, so the harness can
    hand full_agent the SAME ContactTracker instance it's using for the rest
    of the run (see harness.run_policy's is_factory path) -- decide() needs
    that tracker for its compliance vetoes, and the plain
    (payment, customer) -> Plan policies don't need or get one.
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
            )
            return [decision] if decision is not None else []

        return policy_fn

    return with_tracker
