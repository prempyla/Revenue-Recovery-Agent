"""Payment generator. Schema §2.

decline_reason is drawn conditioned on instrument_type (a card can't get
MANDATE_REVOKED) from the weighted category distribution in config.py,
renormalized over whichever reasons are valid for that instrument.
"""

from datetime import datetime, timedelta
from typing import List, Optional, Sequence

import numpy as np

from . import config
from .types import Customer, DeclineReason, InstrumentType, Payment


def _sample_instrument_type(rng: np.random.Generator) -> InstrumentType:
    types = list(config.INSTRUMENT_TYPE_WEIGHTS.keys())
    weights = list(config.INSTRUMENT_TYPE_WEIGHTS.values())
    return InstrumentType(rng.choice(types, p=weights))


def _sample_decline_reason(
    rng: np.random.Generator, instrument_type: InstrumentType
) -> DeclineReason:
    valid = config.INSTRUMENT_VALID_DECLINE_REASONS[instrument_type.value]
    weights = np.array([config.DECLINE_REASON_WEIGHTS[r] for r in valid], dtype=float)
    weights = weights / weights.sum()
    return DeclineReason(rng.choice(valid, p=weights))


def _sample_failed_at(
    rng: np.random.Generator, window_start: datetime, window_days: int
) -> datetime:
    offset_seconds = rng.uniform(0, window_days * 24 * 3600)
    return window_start + timedelta(seconds=float(offset_seconds))


def generate_payments(
    customers: Sequence[Customer],
    n: int = 450,
    window_start: datetime = datetime(2026, 1, 1),
    window_days: int = 30,
    seed: Optional[int] = None,
) -> List[Payment]:
    """Generate n at-risk payment records per schema §2, spread across the window."""
    rng = np.random.default_rng(seed)
    customer_ids = [c.customer_id for c in customers]
    payments = []
    for i in range(1, n + 1):
        instrument_type = _sample_instrument_type(rng)
        payments.append(
            Payment(
                payment_id=f"pay_{i:05d}",
                customer_id=str(rng.choice(customer_ids)),
                amount_paise=int(
                    rng.integers(config.AMOUNT_PAISE_MIN, config.AMOUNT_PAISE_MAX + 1)
                ),
                instrument_type=instrument_type,
                decline_reason=_sample_decline_reason(rng, instrument_type),
                issuer_code=str(rng.choice(config.ISSUER_CODES)),
                failed_at=_sample_failed_at(rng, window_start, window_days),
            )
        )
    return payments
