"""Customer persona generator. Schema §1.

Every field, type, and distribution below matches docs/simulator_schema.md §1
exactly. Ground truth (recovery_propensity, funds_arrival_day, ...) is hidden
from the policy under test — only the eval harness and this module see it.
"""

from typing import List, Optional

import numpy as np

from . import config
from .types import Customer, Language


def _sample_funds_arrival_day(rng: np.random.Generator) -> int:
    if rng.random() < config.FUNDS_ARRIVAL_EARLY_PROBABILITY:
        low, high = config.FUNDS_ARRIVAL_EARLY_RANGE
    else:
        low, high = config.FUNDS_ARRIVAL_LATE_RANGE
    return int(rng.integers(low, high + 1))


def _sample_contact_hours(rng: np.random.Generator) -> tuple:
    if rng.random() < config.CONTACT_HOURS_NARROW_PROBABILITY:
        return config.CONTACT_HOURS_NARROW
    return config.CONTACT_HOURS_DEFAULT


def _sample_annoyance_threshold(rng: np.random.Generator) -> int:
    return max(
        config.ANNOYANCE_THRESHOLD_FLOOR,
        int(rng.poisson(config.ANNOYANCE_THRESHOLD_POISSON_LAMBDA)),
    )


def _sample_channel_response_rate(rng: np.random.Generator) -> dict:
    lo, hi = config.CHANNEL_RESPONSE_RATE_MIN, config.CHANNEL_RESPONSE_RATE_MAX
    return {
        "upi_link": float(rng.uniform(lo, hi)),
        "whatsapp": float(rng.uniform(lo, hi)),
        "sms": float(rng.uniform(lo, hi)),
    }


def _sample_preferred_language(rng: np.random.Generator) -> Language:
    languages = list(config.LANGUAGE_WEIGHTS.keys())
    weights = list(config.LANGUAGE_WEIGHTS.values())
    return Language(rng.choice(languages, p=weights))


def generate_customers(n: int = 300, seed: Optional[int] = None) -> List[Customer]:
    """Generate n customer personas per schema §1."""
    rng = np.random.default_rng(seed)
    customers = []
    for i in range(1, n + 1):
        customers.append(
            Customer(
                customer_id=f"cust_{i:04d}",
                recovery_propensity=float(rng.beta(2, 2)),
                funds_arrival_day=_sample_funds_arrival_day(rng),
                contact_hours=_sample_contact_hours(rng),
                annoyance_threshold=_sample_annoyance_threshold(rng),
                channel_response_rate=_sample_channel_response_rate(rng),
                preferred_language=_sample_preferred_language(rng),
            )
        )
    return customers
