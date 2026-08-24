"""Per-call token/cost accounting. Verified against Anthropic's own
Claude Sonnet 4.6 announcement (anthropic.com/news/claude-sonnet-4-6),
checked 2026-08-25: "$3/$15 per million tokens" (input/output) — matches
the claude-api skill's cached table. See DECISIONS.md 2026-08-25 for the
citation; this number goes in the video, so it's checked against a
published source, not assumed from the skill alone.
"""

from dataclasses import dataclass, field
from typing import List

from .types import UsageRecord

# Reported in USD, not paise/rupees, deliberately: converting would bake in
# an FX rate this spec never asked for, and Anthropic bills in USD anyway.
INPUT_COST_USD_PER_MILLION_TOKENS = 3.00
OUTPUT_COST_USD_PER_MILLION_TOKENS = 15.00

DEFAULT_MONTHLY_PAYMENT_VOLUME = 10_000_000  # requirement 3's stated extrapolation target


def _call_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000 * INPUT_COST_USD_PER_MILLION_TOKENS
        + output_tokens / 1_000_000 * OUTPUT_COST_USD_PER_MILLION_TOKENS
    )


@dataclass
class CostTracker:
    """Accumulates UsageRecord entries across a run. used_llm=False records
    (fallback path taken) contribute zero cost but are still counted, so the
    fallback rate is visible alongside the cost."""

    records: List[UsageRecord] = field(default_factory=list)

    def record(self, usage: UsageRecord) -> None:
        self.records.append(usage)

    def total_cost_usd(self) -> float:
        return sum(
            _call_cost_usd(r.input_tokens, r.output_tokens) for r in self.records if r.used_llm
        )

    def total_calls(self) -> int:
        return len(self.records)

    def llm_calls(self) -> int:
        return sum(1 for r in self.records if r.used_llm)

    def fallback_calls(self) -> int:
        return sum(1 for r in self.records if not r.used_llm)

    def fallback_rate(self) -> float:
        return self.fallback_calls() / self.total_calls() if self.total_calls() else 0.0

    def cost_per_payment_usd(self, n_payments: int) -> float:
        return self.total_cost_usd() / n_payments if n_payments else 0.0

    def extrapolated_monthly_cost_usd(
        self, n_payments_in_sample: int, monthly_payment_volume: int = DEFAULT_MONTHLY_PAYMENT_VOLUME
    ) -> float:
        return self.cost_per_payment_usd(n_payments_in_sample) * monthly_payment_volume

    def report(self, n_payments: int, monthly_payment_volume: int = DEFAULT_MONTHLY_PAYMENT_VOLUME) -> dict:
        return {
            "total_calls": self.total_calls(),
            "llm_calls": self.llm_calls(),
            "fallback_calls": self.fallback_calls(),
            "fallback_rate": self.fallback_rate(),
            "total_cost_usd": self.total_cost_usd(),
            "cost_per_payment_usd": self.cost_per_payment_usd(n_payments),
            "extrapolated_monthly_cost_usd": self.extrapolated_monthly_cost_usd(
                n_payments, monthly_payment_volume
            ),
            "monthly_payment_volume_assumed": monthly_payment_volume,
        }
