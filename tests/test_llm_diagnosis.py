"""explain_diagnosis: happy path, full fallback, client=None ablation."""

from llm import CostTracker, FakeLLMClient, explain_diagnosis
from llm.diagnosis import _fixed_format_diagnosis

CONTEXT = {
    "decline_reason": "ISSUER_DOWN",
    "action_type": "retry_scheduled",
    "offset_minutes": 130.0,
    "estimated_success_rate": 0.5,
    "cost_paise": 0,
}


def test_happy_path_returns_llm_text_and_records_cost():
    client = FakeLLMClient()
    client.diagnosis_response = "Held the retry because a systemic outage was detected."
    cost = CostTracker()
    result = explain_diagnosis(CONTEXT, client, cost)
    assert result == client.diagnosis_response
    assert cost.llm_calls() == 1


def test_falls_back_to_fixed_format_on_exception():
    client = FakeLLMClient()
    client.raise_on_call = RuntimeError("boom")
    cost = CostTracker()
    result = explain_diagnosis(CONTEXT, client, cost)
    assert result == _fixed_format_diagnosis(CONTEXT)
    assert cost.fallback_calls() == 1


def test_falls_back_when_response_is_empty():
    client = FakeLLMClient()
    client.diagnosis_response = ""
    result = explain_diagnosis(CONTEXT, client, None)
    assert result == _fixed_format_diagnosis(CONTEXT)


def test_client_none_is_a_deliberate_ablation():
    cost = CostTracker()
    result = explain_diagnosis(CONTEXT, None, cost)
    assert result == _fixed_format_diagnosis(CONTEXT)
    assert cost.total_calls() == 0


def test_fixed_format_never_raises_on_missing_optional_context_fields():
    minimal_context = {"decline_reason": "NETWORK_TIMEOUT", "action_type": "retry_now"}
    result = _fixed_format_diagnosis(minimal_context)
    assert "NETWORK_TIMEOUT" in result
    assert "retry_now" in result
