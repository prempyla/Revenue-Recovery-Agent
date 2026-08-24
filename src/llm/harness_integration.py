"""Runs the LLM layer (drafting + diagnosis) as a pass OVER an
already-computed full_agent RunResult — decide() has already run by the
time this executes, so nothing here can influence which action was chosen
or its timing. Gated to customer-facing log entries only (requirement 3:
"do not call it on payments where the action is a silent retry with no
customer contact").

Kept structurally separate from simulator/harness.py (llm/ imports
simulator/, never the reverse) so the core eval harness stays LLM-agnostic.
See report.py for how this composes with simulator.run_eval() for the full
3-seed comparison.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from simulator.harness import RunResult
from simulator.types import Customer

from .client import LLMClientInterface
from .cost_tracking import CostTracker
from .diagnosis import explain_diagnosis
from .drafting import draft_message


@dataclass(frozen=True)
class LLMLayerOutput:
    payment_id: str
    drafted_message: str
    diagnosis_text: str


def run_llm_layer(
    result: RunResult,
    customers_by_id: Dict[str, Customer],
    client: Optional[LLMClientInterface],
    cost_tracker: CostTracker,
    merchant_name: str = "Acme Merchant",
) -> List[LLMLayerOutput]:
    """client=None runs the deliberate-ablation path (full_agent_minus_llm):
    every call goes straight to its template/fixed-format fallback, zero
    cost recorded, no network attempt — see drafting.py/diagnosis.py."""
    if not result.implemented:
        return []

    payments_by_id = {o.payment.payment_id: o.payment for o in result.outcomes}
    outputs = []
    for entry in result.log_entries:
        if not entry.is_customer_facing:
            continue
        payment = payments_by_id[entry.payment_id]
        customer = customers_by_id[payment.customer_id]

        draft_context = {
            "decline_reason": payment.decline_reason.value,
            "action_type": entry.action_type.value,
            "amount_paise": payment.amount_paise,
            "preferred_language": customer.preferred_language.value,
            "merchant_name": merchant_name,
        }
        message = draft_message(draft_context, client, cost_tracker)

        diagnosis_context = {
            "decline_reason": payment.decline_reason.value,
            "action_type": entry.action_type.value,
            "cost_paise": entry.cost_paise,
        }
        diagnosis = explain_diagnosis(diagnosis_context, client, cost_tracker)

        outputs.append(LLMLayerOutput(entry.payment_id, message, diagnosis))
    return outputs
