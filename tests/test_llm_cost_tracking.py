"""CostTracker: per-call cost math and monthly extrapolation. Pricing
verified against Anthropic's Sonnet 4.6 announcement 2026-08-25:
$3/$15 per 1M input/output tokens -- see DECISIONS.md."""

from llm import CostTracker, UsageRecord


def test_cost_math_matches_published_pricing():
    tracker = CostTracker()
    tracker.record(UsageRecord("draft_message", used_llm=True, input_tokens=1_000_000, output_tokens=0))
    assert tracker.total_cost_usd() == 3.00

    tracker2 = CostTracker()
    tracker2.record(UsageRecord("draft_message", used_llm=True, input_tokens=0, output_tokens=1_000_000))
    assert tracker2.total_cost_usd() == 15.00


def test_fallback_records_contribute_zero_cost_but_are_still_counted():
    tracker = CostTracker()
    tracker.record(UsageRecord("classify_reply", used_llm=False, fallback_reason="timeout"))
    assert tracker.total_cost_usd() == 0.0
    assert tracker.total_calls() == 1
    assert tracker.fallback_calls() == 1
    assert tracker.llm_calls() == 0
    assert tracker.fallback_rate() == 1.0


def test_mixed_llm_and_fallback_calls():
    tracker = CostTracker()
    tracker.record(UsageRecord("draft_message", used_llm=True, input_tokens=1000, output_tokens=200))
    tracker.record(UsageRecord("draft_message", used_llm=False, fallback_reason="malformed"))
    tracker.record(UsageRecord("explain_diagnosis", used_llm=True, input_tokens=500, output_tokens=100))
    assert tracker.total_calls() == 3
    assert tracker.llm_calls() == 2
    assert tracker.fallback_calls() == 1
    assert tracker.fallback_rate() == 1 / 3


def test_cost_per_payment_and_monthly_extrapolation():
    tracker = CostTracker()
    for _ in range(10):
        tracker.record(UsageRecord("draft_message", used_llm=True, input_tokens=100_000, output_tokens=20_000))
    per_call_cost = 100_000 / 1_000_000 * 3.00 + 20_000 / 1_000_000 * 15.00
    expected_total = per_call_cost * 10

    assert tracker.total_cost_usd() == expected_total

    n_payments_in_sample = 100
    expected_per_payment = expected_total / n_payments_in_sample
    assert tracker.cost_per_payment_usd(n_payments_in_sample) == expected_per_payment

    monthly_volume = 10_000_000
    expected_monthly = expected_per_payment * monthly_volume
    assert tracker.extrapolated_monthly_cost_usd(n_payments_in_sample, monthly_volume) == expected_monthly


def test_report_shape_has_every_required_field():
    tracker = CostTracker()
    tracker.record(UsageRecord("draft_message", used_llm=True, input_tokens=1000, output_tokens=200))
    report = tracker.report(n_payments=1)
    for key in (
        "total_calls", "llm_calls", "fallback_calls", "fallback_rate",
        "total_cost_usd", "cost_per_payment_usd", "extrapolated_monthly_cost_usd",
        "monthly_payment_volume_assumed",
    ):
        assert key in report


def test_empty_tracker_does_not_divide_by_zero():
    tracker = CostTracker()
    assert tracker.total_cost_usd() == 0.0
    assert tracker.fallback_rate() == 0.0
    assert tracker.cost_per_payment_usd(0) == 0.0
    assert tracker.report(n_payments=0)["cost_per_payment_usd"] == 0.0
