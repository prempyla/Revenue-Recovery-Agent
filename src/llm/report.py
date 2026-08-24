"""Composes simulator.run_eval() (decision metrics, LLM-agnostic) with the
real LLM layer (drafting/diagnosis cost + output) to produce the actual
full_agent vs full_agent_minus_llm comparison.

Expected result, stated up front per DECISIONS.md 2026-08-25: IDENTICAL
decision metrics (Rs/contact, recovery rate, invariants) between the two --
ground_truth.py has no message-quality parameter for drafted copy to move,
and decide() never touches the LLM at all. That identity is the proof of
the architectural boundary, not a gap in it: "identical by construction,
not by omission." What genuinely differs is LLM cost (real vs zero) and
the actual message/diagnosis text (LLM-authored vs template).
"""

from typing import Optional, Sequence

from simulator import Customer, OutageEvent, Payment, run_eval, run_policy
from simulator.full_agent import make_full_agent_policy

from .client import LLMClientInterface
from .cost_tracking import CostTracker
from .harness_integration import run_llm_layer


def run_llm_augmented_eval(
    payments: Sequence[Payment],
    customers: Sequence[Customer],
    outage_events: Sequence[OutageEvent],
    seed: int,
    llm_client: Optional[LLMClientInterface],
) -> dict:
    """Runs the full simulator comparison unchanged, then replaces
    full_agent / full_agent_minus_llm's cost and sample output with a real
    LLM-layer pass (run twice against the SAME underlying full_agent
    decision run — once with llm_client, once with None) instead of the
    simulator-only dict-copy placeholder."""
    results = run_eval(payments, customers, outage_events, seed=seed)

    customers_by_id = {c.customer_id: c for c in customers}
    factory = make_full_agent_policy(payments, outage_events, disable_outage_detection=False)
    full_agent_run = run_policy(
        "full_agent", factory, payments, customers_by_id, outage_events, seed, is_factory=True
    )

    with_llm_cost = CostTracker()
    without_llm_cost = CostTracker()
    llm_outputs = run_llm_layer(full_agent_run, customers_by_id, llm_client, with_llm_cost)
    template_outputs = run_llm_layer(full_agent_run, customers_by_id, None, without_llm_cost)

    n_payments = len(payments)
    results["full_agent"]["llm_cost_report"] = with_llm_cost.report(n_payments)
    results["full_agent"]["sample_drafted_messages"] = [o.drafted_message for o in llm_outputs[:3]]

    results["full_agent_minus_llm"]["llm_cost_report"] = without_llm_cost.report(n_payments)
    results["full_agent_minus_llm"]["sample_drafted_messages"] = [
        o.drafted_message for o in template_outputs[:3]
    ]
    results["full_agent_minus_llm"]["note"] = (
        "identical to full_agent on every decision metric (Rs/contact, recovery rate, "
        "invariants) -- by construction, not by omission: decide() never touches the LLM, "
        "and ground_truth.py has no message-quality parameter for drafted copy to move. "
        "Differs only in LLM cost (zero here) and message/diagnosis text (template vs LLM)."
    )

    return results
