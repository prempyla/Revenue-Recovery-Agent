"""The load-bearing test: run the ENTIRE 3-seed harness comparison with an
LLM client that raises on every single call, and confirm it completes and
produces the same policy ranking as with a well-behaved client. This is the
actual proof of "full fallback, not degraded" -- if this test passes, the
architecture claim (the pipeline degrades gracefully if the model API is
unavailable) is demonstrated end to end, not just asserted per-function."""

from datetime import datetime

from llm import FakeLLMClient, run_llm_augmented_eval
from simulator import generate_customers, generate_outage_events, generate_payments, inject_outage_events

WINDOW_START = datetime(2026, 1, 1)


def _batch(seed):
    customers = generate_customers(300, seed=seed)
    payments = generate_payments(customers, 450, window_start=WINDOW_START, window_days=30, seed=seed)
    events = generate_outage_events(window_start=WINDOW_START, window_days=30, seed=seed)
    payments = inject_outage_events(payments, customers, events, seed=seed)
    return customers, payments, events


def _rank(results):
    """(policy_name, rupees_per_contact) sorted descending -- what "same
    policy ranking" means concretely."""
    ranked = [
        (name, m["primary_rupees_per_contact"])
        for name, m in results.items()
        if m["implemented"] and m["primary_rupees_per_contact"] is not None
    ]
    return sorted(ranked, key=lambda kv: -kv[1])


def test_full_3_seed_comparison_completes_with_a_client_that_always_raises():
    for seed in (42, 7, 123):
        customers, payments, events = _batch(seed)

        working_client = FakeLLMClient()
        working_client.draft_response = "Hi {{customer_name}}, pay here: {{payment_link}}"
        working_client.diagnosis_response = "Scheduled."
        results_working = run_llm_augmented_eval(payments, customers, events, seed=7, llm_client=working_client)

        raising_client = FakeLLMClient()
        raising_client.raise_on_call = TimeoutError("simulated total LLM outage")
        results_raising = run_llm_augmented_eval(payments, customers, events, seed=7, llm_client=raising_client)

        # The run completed (no exception propagated) and produced the
        # exact same decision metrics -- fallback text differs, but nothing
        # about WHICH actions were taken or their outcomes changed.
        for name in ("do_nothing", "naive_fixed_retry", "rules_only", "full_agent", "full_agent_minus_outage_detection"):
            assert results_raising[name]["implemented"] == results_working[name]["implemented"], (seed, name)
            assert (
                results_raising[name]["primary_rupees_per_contact"]
                == results_working[name]["primary_rupees_per_contact"]
            ), (seed, name)
            assert (
                results_raising[name]["total_recovered_rupees"]
                == results_working[name]["total_recovered_rupees"]
            ), (seed, name)

        # Same policy ranking, seed by seed.
        assert _rank(results_raising) == _rank(results_working), seed

        # And the raising client genuinely fell back for every call --
        # this isn't passing because nothing was attempted.
        cost_report = results_raising["full_agent"]["llm_cost_report"]
        assert cost_report["total_calls"] > 0, "fallback path must actually have been exercised"
        assert cost_report["llm_calls"] == 0
        assert cost_report["fallback_calls"] == cost_report["total_calls"]
        assert cost_report["total_cost_usd"] == 0
        assert len(results_raising["full_agent"]["sample_drafted_messages"]) > 0  # fallback text was produced
