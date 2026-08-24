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


def test_full_agent_and_ablations_are_implemented_and_run_without_crashing():
    customers, payments, events = _sample_batch()
    results = run_eval(payments, customers, events, seed=1)
    for name in ("full_agent", "full_agent_minus_outage_detection", "full_agent_minus_llm"):
        assert results[name]["implemented"] is True, name


def test_full_agent_never_violates_hard_compliance_invariants_on_any_seed():
    """decide()'s vetoes (contact hours, weekly cap/opt-out proxy) run
    BEFORE scoring -- confirms that holds up as zero violations in the
    harness's own post-hoc audit, across seeds, not just by construction."""
    for seed in (42, 7, 123):
        customers = generate_customers(300, seed=seed)
        payments = generate_payments(customers, 450, window_start=WINDOW_START, window_days=30, seed=seed)
        events = generate_outage_events(window_start=WINDOW_START, window_days=30, seed=seed)
        payments = inject_outage_events(payments, customers, events, seed=seed)

        results = run_eval(payments, customers, events, seed=7)
        invariants = results["full_agent"]["invariants"]
        assert invariants["zero_contacts_outside_allowed_hours"] is True, seed
        assert invariants["zero_weekly_cap_violations"] is True, seed
        assert invariants["zero_double_charges"] is True, seed


def test_full_agent_beats_rules_only_on_rupees_per_contact_on_all_seeds():
    """Eval spec §4 falsification check: if rules_only >= full_agent on
    Rs/contact, the decision-policy layer adds nothing over static taxonomy
    routing. Must hold across seeds, not just one."""
    for seed in (42, 7, 123):
        customers = generate_customers(300, seed=seed)
        payments = generate_payments(customers, 450, window_start=WINDOW_START, window_days=30, seed=seed)
        events = generate_outage_events(window_start=WINDOW_START, window_days=30, seed=seed)
        payments = inject_outage_events(payments, customers, events, seed=seed)

        results = run_eval(payments, customers, events, seed=7)
        assert (
            results["full_agent"]["primary_rupees_per_contact"]
            > results["rules_only"]["primary_rupees_per_contact"]
        ), seed


def test_outage_detection_ablation_measurably_differs_from_full_agent_on_all_seeds():
    """Regression guard: an earlier version of full_agent's hold duration
    (60min: detector window+cooldown+30) wasn't long enough to clear a
    90-minute true_outage for payments failing near its onset, so on 2 of 3
    seeds the ablation showed ZERO measurable difference from full_agent --
    not because outage detection didn't matter, but because both the held
    and un-held retry times landed inside the same still-live outage
    window. Fixed via ASSUMED_MAX_OUTAGE_DURATION_MINUTES (full_agent.py).
    This must keep holding across seeds, not just one."""
    for seed in (42, 7, 123):
        customers = generate_customers(300, seed=seed)
        payments = generate_payments(customers, 450, window_start=WINDOW_START, window_days=30, seed=seed)
        events = generate_outage_events(window_start=WINDOW_START, window_days=30, seed=seed)
        payments = inject_outage_events(payments, customers, events, seed=seed)

        results = run_eval(payments, customers, events, seed=7)
        full_agent = results["full_agent"]
        ablation = results["full_agent_minus_outage_detection"]

        assert full_agent["total_recovered_rupees"] > ablation["total_recovered_rupees"], seed


def test_full_agent_minus_llm_is_identical_to_full_agent_with_a_note():
    """No LLM layer exists yet -- eval spec §3's minus-LLM ablation is
    trivially the same result this round, and says so rather than faking a
    separate measurement."""
    customers, payments, events = _sample_batch()
    results = run_eval(payments, customers, events, seed=1)
    full_agent = results["full_agent"]
    minus_llm = results["full_agent_minus_llm"]
    assert minus_llm["primary_rupees_per_contact"] == full_agent["primary_rupees_per_contact"]
    assert minus_llm["total_recovered_rupees"] == full_agent["total_recovered_rupees"]
    assert "note" in minus_llm and "no LLM layer" in minus_llm["note"]


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


def test_single_action_policies_never_double_charge():
    """do_nothing (0 actions/payment) and rules_only (at most 1 action/
    payment) can never log two successes for the same payment_id — that
    requires a multi-attempt plan, which only naive_fixed_retry has."""
    customers, payments, events = _sample_batch()
    results = run_eval(payments, customers, events, seed=1)
    for name in ("do_nothing", "rules_only"):
        assert results[name]["invariants"]["zero_double_charges"] is True


def test_naive_fixed_retry_can_double_charge_on_a_real_batch():
    """naive fires all 3 attempts regardless of prior outcome (unlike
    rules_only/do_nothing), so on a large enough batch some payments will
    log more than one independent-Bernoulli-draw success. This is expected —
    it's exactly what the invariant exists to catch, not a harness bug."""
    customers, payments, events = _sample_batch()
    results = run_eval(payments, customers, events, seed=1)
    invariants = results["naive_fixed_retry"]["invariants"]
    assert invariants["zero_double_charges"] is False
    assert invariants["double_charge_count"] > 0


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


def _fake_run_result(customer_id: str, action_times, opted_out_at=None) -> RunResult:
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
    return RunResult(
        policy_name="test",
        implemented=True,
        log_entries=log_entries,
        outcomes=outcomes,
        opted_out_at=opted_out_at,
    )


def test_explicit_opt_out_invariant_flags_contacts_at_or_after_the_opt_out_timestamp():
    persona = _weekly_cap_persona()
    base = datetime(2026, 1, 1, 10)
    times = [base, base + timedelta(hours=1), base + timedelta(hours=2)]
    # Opted out between the 2nd and 3rd contact -- only the 3rd is a violation.
    opted_out_at = {persona.customer_id: base + timedelta(hours=1, minutes=30)}
    result = _fake_run_result(persona.customer_id, times, opted_out_at=opted_out_at)
    invariants = harness_module._check_invariants(result, {persona.customer_id: persona})
    assert invariants["zero_contacts_after_explicit_opt_out"] is False
    assert invariants["contacts_after_explicit_opt_out_count"] == 1


def test_explicit_opt_out_invariant_is_zero_when_no_opt_out_recorded():
    persona = _weekly_cap_persona()
    base = datetime(2026, 1, 1, 10)
    times = [base, base + timedelta(hours=1)]
    result = _fake_run_result(persona.customer_id, times, opted_out_at=None)
    invariants = harness_module._check_invariants(result, {persona.customer_id: persona})
    assert invariants["zero_contacts_after_explicit_opt_out"] is True
    assert invariants["contacts_after_explicit_opt_out_count"] == 0


def test_explicit_opt_out_invariant_is_distinct_from_annoyance_threshold_invariant():
    """A customer can be flagged by one and not the other -- they measure
    different things (stated request vs hidden persona patience)."""
    persona = _weekly_cap_persona()  # annoyance_threshold=10, won't trip on 2 contacts
    base = datetime(2026, 1, 1, 10)
    times = [base, base + timedelta(hours=1)]
    opted_out_at = {persona.customer_id: base}  # opted out immediately
    result = _fake_run_result(persona.customer_id, times, opted_out_at=opted_out_at)
    invariants = harness_module._check_invariants(result, {persona.customer_id: persona})
    assert invariants["zero_contacts_after_opt_out"] is True  # persona patience never exceeded
    assert invariants["zero_contacts_after_explicit_opt_out"] is False  # but they DID say stop
    assert invariants["contacts_after_explicit_opt_out_count"] == 2


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


def test_naive_double_charge_is_logged_fully_but_revenue_counted_once():
    """Deterministic proof: force ground truth to certain success (prob=1.0
    on every attempt) so naive's 3 independent Bernoulli draws all come back
    'success'. The action log must show all 3 (audit trail stays complete),
    the double_charge invariant must flag exactly this payment, and
    total_recovered must still count the payment's amount exactly once."""
    from unittest.mock import patch

    from simulator.policies import naive_fixed_retry_policy

    persona = _weekly_cap_persona()  # annoyance_threshold=10, contact_hours=(0,24) — won't interfere
    payment = Payment(
        payment_id="pay_certain",
        customer_id=persona.customer_id,
        amount_paise=500_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=DeclineReason.NETWORK_TIMEOUT,
        issuer_code="HDFC",
        failed_at=datetime(2026, 1, 1),
    )
    customers_by_id = {persona.customer_id: persona}

    with patch.object(harness_module, "success_probability", return_value=1.0):
        result = harness_module.run_policy(
            "naive_fixed_retry", naive_fixed_retry_policy, [payment], customers_by_id, [], seed=1
        )

    success_entries = [e for e in result.log_entries if e.outcome == "success"]
    assert len(success_entries) == 3  # every attempt logged, none suppressed

    invariants = harness_module._check_invariants(result, customers_by_id)
    assert invariants["zero_double_charges"] is False
    assert invariants["double_charge_count"] == 1  # one payment_id over-counted, not one per attempt

    metrics = harness_module.compute_metrics(result, [], customers_by_id)
    assert metrics["total_recovered_rupees"] == payment.amount_paise / 100.0  # counted once, not 3x


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
