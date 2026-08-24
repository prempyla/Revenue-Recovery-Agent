"""Isolation tests for the eval harness. Schema §7 step 7."""

from datetime import datetime

from simulator import (
    generate_customers,
    generate_outage_events,
    generate_payments,
    inject_outage_events,
    run_eval,
)

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
            "weekly_contact_cap",
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
