"""Eval harness. Eval spec §2 (metrics), schema §6 (log entry), §7 step 7.

Drives a policy's plan against ground truth for a batch of payments, and
computes every metric eval_protocol_and_simulator_spec.md §2 asks for:
primary (Rs/contact), secondary (recovery rate, time-to-recovery, outage
recovery rate, systemic-detector rate/lag), and the hard pass/fail invariants.

Systemic-detector metrics require a policy that actually does outage
detection — none of the three implemented baselines do (that's a full_agent
feature), so those two fields report None rather than a fabricated number.
full_agent itself is caught as NotImplementedError and reported as such
rather than crashing the run — see run_policy() and run_eval().
"""

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

import numpy as np

from . import config
from .contact_tracking import ContactTracker, contact_cost_paise, is_customer_facing
from .ground_truth import success_probability
from .policies import POLICIES, Plan
from .types import ActionType, Customer, OutageEvent, Payment

RECOVERY_HORIZON_DAYS = 7  # eval spec §2: "within a fixed horizon (e.g. 7 days)"


@dataclass(frozen=True)
class LogEntry:
    """§6 action log entry."""

    log_id: str
    payment_id: str
    action_type: ActionType
    channel: str  # "none" for no-channel actions, per §6
    action_time: datetime
    is_customer_facing: bool
    policy_name: str
    reason: str
    outcome: str  # "success" | "fail" | "pending"
    cost_paise: int


@dataclass(frozen=True)
class PaymentOutcome:
    payment: Payment
    recovered: bool
    recovered_at: Optional[datetime]


@dataclass
class RunResult:
    policy_name: str
    implemented: bool
    log_entries: Optional[List[LogEntry]] = None
    outcomes: Optional[List[PaymentOutcome]] = None
    error: Optional[str] = None


def _reason_for(policy_name: str, payment: Payment, attempt_index: int, plan_len: int) -> str:
    if policy_name == "naive_fixed_retry":
        return f"fixed retry attempt {attempt_index + 1}/{plan_len}, no taxonomy lookup"
    if policy_name == "rules_only":
        return f"taxonomy routing for {payment.decline_reason.value}"
    return f"{policy_name} action {attempt_index + 1}/{plan_len}"


def run_policy(
    policy_name: str,
    policy_fn,
    payments: Sequence[Payment],
    customers_by_id: Dict[str, Customer],
    outage_events: Sequence[OutageEvent],
    seed: int,
) -> RunResult:
    """Run one policy across a batch of payments. Each policy gets its own
    ContactTracker and RNG stream — same underlying ground-truth batch, but
    an independent simulated run per policy, per eval spec §3 ("same batch,
    same ground truth")."""
    rng = np.random.default_rng(seed)
    tracker = ContactTracker()
    log_entries: List[LogEntry] = []
    outcomes: List[PaymentOutcome] = []
    log_id_counter = 0

    for payment in payments:
        customer = customers_by_id[payment.customer_id]
        try:
            plan: Plan = policy_fn(payment, customer)
        except NotImplementedError as exc:
            return RunResult(policy_name=policy_name, implemented=False, error=str(exc))

        recovered = False
        recovered_at = None
        for attempt_index, (offset, action) in enumerate(plan):
            action_time = payment.failed_at + offset
            contact_count_before = tracker.contact_count(customer.customer_id)
            probability = success_probability(
                payment, customer, action, action_time, contact_count_before, outage_events
            )
            success = bool(rng.random() < probability)
            customer_facing = is_customer_facing(action)

            if customer_facing:
                tracker.record(customer.customer_id, action)

            log_id_counter += 1
            log_entries.append(
                LogEntry(
                    log_id=f"log_{policy_name}_{log_id_counter:06d}",
                    payment_id=payment.payment_id,
                    action_type=action.action_type,
                    channel=action.channel.value if action.channel else "none",
                    action_time=action_time,
                    is_customer_facing=customer_facing,
                    policy_name=policy_name,
                    reason=_reason_for(policy_name, payment, attempt_index, len(plan)),
                    outcome="success" if success else "fail",
                    cost_paise=contact_cost_paise(action),
                )
            )

            if success:
                recovered = True
                recovered_at = action_time
                break  # stop the plan early once recovered

        outcomes.append(
            PaymentOutcome(payment=payment, recovered=recovered, recovered_at=recovered_at)
        )

    return RunResult(
        policy_name=policy_name, implemented=True, log_entries=log_entries, outcomes=outcomes
    )


def _true_outage_events(outage_events: Sequence[OutageEvent]) -> List[OutageEvent]:
    return [e for e in outage_events if e.kind == "true_outage"]


def _check_invariants(result: RunResult, customers_by_id: Dict[str, Customer]) -> dict:
    log_entries = result.log_entries
    outcomes = result.outcomes
    payments_by_id = {o.payment.payment_id: o.payment for o in outcomes}

    # Zero double-charges: no payment should have more than one success
    # logged against it. The harness stops a plan on first success, so this
    # checks that guarantee actually held rather than assuming it.
    success_counts: Dict[str, int] = {}
    for e in log_entries:
        if e.outcome == "success":
            success_counts[e.payment_id] = success_counts.get(e.payment_id, 0) + 1
    double_charges = sum(1 for c in success_counts.values() if c > 1)

    # Zero contacts outside allowed hours: customer-facing entries whose
    # action_time hour falls outside that customer's contact_hours window.
    # Datetimes are treated as IST wall-clock directly (no separate timezone
    # handling exists anywhere else in this simulator either).
    outside_hours_count = 0
    for e in log_entries:
        if not e.is_customer_facing:
            continue
        customer = customers_by_id[payments_by_id[e.payment_id].customer_id]
        start, end = customer.contact_hours
        if not (start <= e.action_time.hour < end):
            outside_hours_count += 1

    # Zero contacts after opt-out: replay contact_count per customer in the
    # same order run_policy recorded them, and check whether any
    # customer-facing entry happened when the count-before already exceeded
    # that customer's annoyance_threshold.
    contact_counts: Dict[str, int] = {}
    after_opt_out_count = 0
    for e in log_entries:
        if not e.is_customer_facing:
            continue
        customer = customers_by_id[payments_by_id[e.payment_id].customer_id]
        count_before = contact_counts.get(customer.customer_id, 0)
        if count_before > customer.annoyance_threshold:
            after_opt_out_count += 1
        contact_counts[customer.customer_id] = count_before + 1

    # Rolling weekly contact cap: for each customer, and each customer-facing
    # action to them, count how many customer-facing actions to that same
    # customer fall within the trailing 7 days up to and including this one.
    # A violation is any action where that count exceeds MAX_WEEKLY_CONTACTS
    # — a rate limit (config.py) distinct from the hidden annoyance_threshold.
    times_by_customer: Dict[str, List[datetime]] = defaultdict(list)
    for e in log_entries:
        if not e.is_customer_facing:
            continue
        customer_id = payments_by_id[e.payment_id].customer_id
        times_by_customer[customer_id].append(e.action_time)

    weekly_cap_violation_count = 0
    window = timedelta(days=7)
    for times in times_by_customer.values():
        times.sort()
        left = 0
        for right in range(len(times)):
            while times[right] - times[left] > window:
                left += 1
            count_in_window = right - left + 1
            if count_in_window > config.MAX_WEEKLY_CONTACTS:
                weekly_cap_violation_count += 1

    return {
        "zero_double_charges": double_charges == 0,
        "double_charge_count": double_charges,
        "zero_contacts_outside_allowed_hours": outside_hours_count == 0,
        "contacts_outside_allowed_hours_count": outside_hours_count,
        "zero_contacts_after_opt_out": after_opt_out_count == 0,
        "contacts_after_opt_out_count": after_opt_out_count,
        "zero_weekly_cap_violations": weekly_cap_violation_count == 0,
        "weekly_cap_violation_count": weekly_cap_violation_count,
    }


def compute_metrics(
    result: RunResult, outage_events: Sequence[OutageEvent], customers_by_id: Dict[str, Customer]
) -> dict:
    """Eval spec §2: primary, secondary, invariants — for one implemented run."""
    if not result.implemented:
        return {"policy_name": result.policy_name, "implemented": False, "error": result.error}

    log_entries = result.log_entries
    outcomes = result.outcomes

    total_recovered_paise = sum(o.payment.amount_paise for o in outcomes if o.recovered)
    contacts_made = sum(1 for e in log_entries if e.is_customer_facing)
    primary_rupees_per_contact = (
        (total_recovered_paise / contacts_made) / 100.0 if contacts_made > 0 else None
    )

    total_at_risk_paise = sum(o.payment.amount_paise for o in outcomes)
    recovered_within_horizon_paise = sum(
        o.payment.amount_paise
        for o in outcomes
        if o.recovered
        and (o.recovered_at - o.payment.failed_at) <= timedelta(days=RECOVERY_HORIZON_DAYS)
    )
    recovery_rate = (
        recovered_within_horizon_paise / total_at_risk_paise if total_at_risk_paise > 0 else None
    )

    recovery_times_hours = [
        (o.recovered_at - o.payment.failed_at).total_seconds() / 3600
        for o in outcomes
        if o.recovered
    ]
    median_time_to_recovery_hours = (
        statistics.median(recovery_times_hours) if recovery_times_hours else None
    )

    true_outages = _true_outage_events(outage_events)
    outage_window_outcomes = [
        o
        for o in outcomes
        if any(
            e.issuer_code == o.payment.issuer_code
            and e.start_time
            <= o.payment.failed_at
            <= e.start_time + timedelta(minutes=e.duration_minutes)
            for e in true_outages
        )
    ]
    outage_window_recovery_rate = (
        sum(1 for o in outage_window_outcomes if o.recovered) / len(outage_window_outcomes)
        if outage_window_outcomes
        else None
    )

    return {
        "policy_name": result.policy_name,
        "implemented": True,
        "primary_rupees_per_contact": primary_rupees_per_contact,
        "total_recovered_rupees": total_recovered_paise / 100.0,
        "contacts_made": contacts_made,
        "recovery_rate_7day": recovery_rate,
        "median_time_to_recovery_hours": median_time_to_recovery_hours,
        "outage_window_recovery_rate": outage_window_recovery_rate,
        "outage_window_payment_count": len(outage_window_outcomes),
        # No baseline this round does systemic outage detection (a
        # full_agent-only feature) — nothing to score a false-positive rate
        # or detection lag against.
        "systemic_detector_false_positive_rate": None,
        "systemic_detector_detection_lag": None,
        "invariants": _check_invariants(result, customers_by_id),
    }


def run_eval(
    payments: Sequence[Payment],
    customers: Sequence[Customer],
    outage_events: Sequence[OutageEvent],
    seed: int = 0,
    policies: Dict[str, "callable"] = POLICIES,
) -> Dict[str, dict]:
    """Run every policy in `policies` over the same batch and return
    {policy_name: metrics_dict}, per schema §7 step 7."""
    customers_by_id = {c.customer_id: c for c in customers}
    results = {}
    for name, policy_fn in policies.items():
        run_result = run_policy(name, policy_fn, payments, customers_by_id, outage_events, seed)
        results[name] = compute_metrics(run_result, outage_events, customers_by_id)
    return results
