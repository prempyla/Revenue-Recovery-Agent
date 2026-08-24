"""Isolation tests for the eval harness. Schema §7 step 7."""

from datetime import datetime, timedelta

from simulator import (
    Action,
    ActionType,
    Channel,
    Customer,
    DeclineReason,
    InstrumentType,
    Language,
    Payment,
    generate_customers,
    generate_outage_events,
    generate_payments,
    inject_outage_events,
    run_eval,
)
from simulator import harness as harness_module
from simulator.harness import LogEntry, PaymentOutcome, RunResult

WINDOW_START = datetime(2026, 1, 1)


def _sample_batch(seed=1):
    customers = generate_customers(60, seed=seed)
    payments = generate_payments(customers, 100, window_start=WINDOW_START, window_days=30, seed=seed)
    events = generate_outage_events(window_start=WINDOW_START, window_days=30, seed=seed)
    payments = inject_outage_events(payments, customers, events, seed=seed)
    return customers, payments, events


def test_full_agent_reports_not_implemented_without_crashing():
    customers, payments, events = _sample_batch()
    results = run_eval(payments, customers, events, seed=1)
    assert results["full_agent"]["implemented"] is False
    assert "error" in results["full_agent"]


def test_three_baselines_are_implemented_and_produce_metrics():
    customers, payments, events = _sample_batch()
    results = run_eval(payments, customers, events, seed=1)
    for name in ("do_nothing", "naive_fixed_retry", "rules_only"):
        assert results[name]["implemented"] is True


def test_every_eval_spec_section2_metric_present_for_implemented_policies():
    customers, payments, events = _sample_batch()
    results = run_eval(payments, customers, events, seed=1)
    expected_keys = {
        "primary_rupees_per_contact",
        "total_recovered_rupees",
        "contacts_made",
        "recovery_rate_7day",
        "median_time_to_recovery_hours",
        "outage_window_recovery_rate",
        "systemic_detector_false_positive_rate",
        "systemic_detector_detection_lag",
        "invariants",
    }
    for name in ("do_nothing", "naive_fixed_retry", "rules_only"):
        assert expected_keys.issubset(results[name].keys())
        invariant_keys = {
            "zero_double_charges",
            "zero_contacts_outside_allowed_hours",
            "zero_contacts_after_opt_out",
            "zero_weekly_cap_violations",
        }
        assert invariant_keys.issubset(results[name]["invariants"].keys())


def test_do_nothing_makes_zero_contacts_and_primary_metric_is_undefined():
    customers, payments, events = _sample_batch()
    results = run_eval(payments, customers, events, seed=1)
    do_nothing = results["do_nothing"]
    assert do_nothing["contacts_made"] == 0
    assert do_nothing["total_recovered_rupees"] == 0.0
    assert do_nothing["primary_rupees_per_contact"] is None  # 0/0 is undefined, not 0


def test_do_nothing_never_violates_any_invariant():
    customers, payments, events = _sample_batch()
    results = run_eval(payments, customers, events, seed=1)
    invariants = results["do_nothing"]["invariants"]
    assert invariants["zero_double_charges"] is True
    assert invariants["zero_contacts_outside_allowed_hours"] is True
    assert invariants["zero_contacts_after_opt_out"] is True


def test_no_policy_ever_double_charges():
    customers, payments, events = _sample_batch()
    results = run_eval(payments, customers, events, seed=1)
    for name in ("do_nothing", "naive_fixed_retry", "rules_only"):
        assert results[name]["invariants"]["zero_double_charges"] is True


def _weekly_cap_persona():
    return Customer(
        customer_id="cust_weekly",
        recovery_propensity=0.6,
        funds_arrival_day=2,
        contact_hours=(0, 24),  # wide open, so the hours invariant never interferes
        annoyance_threshold=10,  # high, so the opt-out invariant never interferes
        channel_response_rate={"upi_link": 0.7, "whatsapp": 0.6, "sms": 0.5},
        preferred_language=Language.EN,
    )


def _fake_run_result(customer_id: str, action_times) -> RunResult:
    payment = Payment(
        payment_id="pay_weekly",
        customer_id=customer_id,
        amount_paise=100_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=DeclineReason.AFA_3DS_DROPOFF,
        issuer_code="HDFC",
        failed_at=action_times[0],
    )
    log_entries = [
        LogEntry(
            log_id=f"log_{i}",
            payment_id=payment.payment_id,
            action_type=ActionType.SEND_NUDGE,
            channel=Channel.SMS.value,
            action_time=t,
            is_customer_facing=True,
            policy_name="test",
            reason="test",
            outcome="fail",
            cost_paise=200,
        )
        for i, t in enumerate(action_times)
    ]
    outcomes = [PaymentOutcome(payment=payment, recovered=False, recovered_at=None)]
    return RunResult(policy_name="test", implemented=True, log_entries=log_entries, outcomes=outcomes)


def test_weekly_cap_violation_detected_when_dense_within_7_days():
    persona = _weekly_cap_persona()
    base = datetime(2026, 1, 1, 10)
    # 4 customer-facing contacts within a single 7-day span; cap is 3.
    times = [base, base + timedelta(days=1), base + timedelta(days=3), base + timedelta(days=6)]
    result = _fake_run_result(persona.customer_id, times)
    invariants = harness_module._check_invariants(result, {persona.customer_id: persona})
    assert invariants["zero_weekly_cap_violations"] is False
    assert invariants["weekly_cap_violation_count"] == 1  # only the 4th contact breaches the cap


def test_weekly_cap_not_violated_when_at_or_under_cap():
    persona = _weekly_cap_persona()
    base = datetime(2026, 1, 1, 10)
    times = [base, base + timedelta(days=1), base + timedelta(days=6)]  # exactly 3, the cap
    result = _fake_run_result(persona.customer_id, times)
    invariants = harness_module._check_invariants(result, {persona.customer_id: persona})
    assert invariants["zero_weekly_cap_violations"] is True
    assert invariants["weekly_cap_violation_count"] == 0


def test_weekly_cap_resets_once_contacts_fall_outside_the_rolling_window():
    persona = _weekly_cap_persona()
    base = datetime(2026, 1, 1, 10)
    # 4 contacts total, but split across two windows more than 7 days apart —
    # no single 7-day span ever contains more than 3.
    times = [
        base,
        base + timedelta(days=1),
        base + timedelta(days=2),
        base + timedelta(days=10),
    ]
    result = _fake_run_result(persona.customer_id, times)
    invariants = harness_module._check_invariants(result, {persona.customer_id: persona})
    assert invariants["zero_weekly_cap_violations"] is True
    assert invariants["weekly_cap_violation_count"] == 0


def test_rules_only_never_retries_a_risk_decline_or_terminal_category():
    """The whole point of taxonomy routing: rules_only must never fire a
    blind retry against a category where ground truth defines retry as
    structurally zero (RISK_DECLINE, the terminal categories)."""
    from simulator import DeclineReason, run_policy
    from simulator.policies import rules_only_policy

    customers, payments, events = _sample_batch()
    customers_by_id = {c.customer_id: c for c in customers}
    result = run_policy("rules_only", rules_only_policy, payments, customers_by_id, events, seed=1)

    no_retry_categories = {
        DeclineReason.RISK_DECLINE,
        DeclineReason.CARD_EXPIRED,
        DeclineReason.CARD_OR_ACCOUNT_BLOCKED,
        DeclineReason.MANDATE_REVOKED,
    }
    payments_by_id = {p.payment_id: p for p in payments}
    for entry in result.log_entries:
        payment = payments_by_id[entry.payment_id]
        if payment.decline_reason in no_retry_categories:
            assert entry.action_type.value not in ("retry_now", "retry_scheduled")
