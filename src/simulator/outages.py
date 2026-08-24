"""Outage + decoy injection. Schema §3.

Three parts:
  - generate_outage_events: produces the two OutageEvent records (one
    true_outage, one decoy_cluster), mid-window, different issuers.
  - inject_outage_events: applies their effect to a payment corpus —
    forces any payment already landing in the true_outage window (for that
    issuer) to ISSUER_DOWN, and adds a burst of extra same-issuer
    ISSUER_DOWN payments inside each window so there's an actual cluster for
    a systemic detector to find. The decoy cluster gets no other special
    treatment: ground_truth.success_probability only checks outage_events for
    kind == "true_outage" (see ground_truth._issuer_down), so decoy-cluster
    payments fall through to ordinary idiosyncratic decay, per §3.
  - _generate_true_outage_burst_payments: an additional front-loaded burst,
    true_outage only, concentrated into the first ~20 minutes rather than
    spread across the full 90-minute duration. Added 2026-08-25 (DECISIONS.md)
    after the outage detector study found the spread-out cluster's per-minute
    density was thinner than the decoy's tight 15-minute burst. Purely
    additive — the decoy path and the ISSUER_DOWN-forcing loop above are
    unchanged.
"""

from dataclasses import replace
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

import numpy as np

from . import config
from .payments import _sample_instrument_type
from .types import Customer, DeclineReason, OutageEvent, Payment


def generate_outage_events(
    issuer_codes: Sequence[str] = config.ISSUER_CODES,
    window_start: datetime = datetime(2026, 1, 1),
    window_days: int = 30,
    seed: Optional[int] = None,
) -> List[OutageEvent]:
    """Generate the two outage events per schema §3: one true_outage, one decoy_cluster."""
    rng = np.random.default_rng(seed)
    true_issuer, decoy_issuer = rng.choice(list(issuer_codes), size=2, replace=False)

    low_frac, high_frac = config.OUTAGE_START_FRACTION_RANGE

    def _mid_window_start() -> datetime:
        frac = rng.uniform(low_frac, high_frac)
        return window_start + timedelta(days=float(frac) * window_days)

    return [
        OutageEvent(
            issuer_code=str(true_issuer),
            start_time=_mid_window_start(),
            duration_minutes=config.TRUE_OUTAGE_DURATION_MINUTES,
            kind="true_outage",
        ),
        OutageEvent(
            issuer_code=str(decoy_issuer),
            start_time=_mid_window_start(),
            duration_minutes=config.DECOY_CLUSTER_DURATION_MINUTES,
            kind="decoy_cluster",
        ),
    ]


def _cluster_size(event: OutageEvent) -> int:
    return (
        config.TRUE_OUTAGE_CLUSTER_SIZE
        if event.kind == "true_outage"
        else config.DECOY_CLUSTER_SIZE
    )


def _generate_cluster_payments(
    event: OutageEvent,
    customers: Sequence[Customer],
    id_prefix: str,
    rng: np.random.Generator,
) -> List[Payment]:
    customer_ids = [c.customer_id for c in customers]
    size = _cluster_size(event)
    payments = []
    for i in range(size):
        offset_seconds = rng.uniform(0, event.duration_minutes * 60)
        payments.append(
            Payment(
                payment_id=f"{id_prefix}_{i:03d}",
                customer_id=str(rng.choice(customer_ids)),
                amount_paise=int(
                    rng.integers(config.AMOUNT_PAISE_MIN, config.AMOUNT_PAISE_MAX + 1)
                ),
                instrument_type=_sample_instrument_type(rng),
                decline_reason=DeclineReason.ISSUER_DOWN,
                issuer_code=event.issuer_code,
                failed_at=event.start_time + timedelta(seconds=float(offset_seconds)),
            )
        )
    return payments


def _generate_true_outage_burst_payments(
    event: OutageEvent,
    customers: Sequence[Customer],
    id_prefix: str,
    rng: np.random.Generator,
) -> List[Payment]:
    """Front-loaded burst for the true_outage event only. Additive on top of
    _generate_cluster_payments's full-duration cluster (called separately in
    inject_outage_events, not a replacement) — concentrates
    TRUE_OUTAGE_BURST_SIZE payments into the first
    TRUE_OUTAGE_BURST_WINDOW_MINUTES of the outage instead of spreading them
    across the full duration_minutes, so the detector's per-minute density
    signal isn't diluted. See DECISIONS.md 2026-08-25. Never called for a
    decoy_cluster event (see inject_outage_events) — the decoy's generation
    path is untouched.
    """
    burst_window_minutes = min(config.TRUE_OUTAGE_BURST_WINDOW_MINUTES, event.duration_minutes)
    customer_ids = [c.customer_id for c in customers]
    payments = []
    for i in range(config.TRUE_OUTAGE_BURST_SIZE):
        offset_seconds = rng.uniform(0, burst_window_minutes * 60)
        payments.append(
            Payment(
                payment_id=f"{id_prefix}_{i:03d}",
                customer_id=str(rng.choice(customer_ids)),
                amount_paise=int(
                    rng.integers(config.AMOUNT_PAISE_MIN, config.AMOUNT_PAISE_MAX + 1)
                ),
                instrument_type=_sample_instrument_type(rng),
                decline_reason=DeclineReason.ISSUER_DOWN,
                issuer_code=event.issuer_code,
                failed_at=event.start_time + timedelta(seconds=float(offset_seconds)),
            )
        )
    return payments


def inject_outage_events(
    payments: Sequence[Payment],
    customers: Sequence[Customer],
    outage_events: Sequence[OutageEvent],
    seed: Optional[int] = None,
) -> List[Payment]:
    """Apply outage_events to a payment corpus: force in-window payments to
    ISSUER_DOWN and append a cluster burst for each event.

    Only true_outage forces existing payments — a decoy_cluster is
    coincidental, not causal, so it doesn't retroactively change any
    payment's decline_reason; its cluster payments are the only effect it has.
    """
    rng = np.random.default_rng(seed)
    true_outages = [e for e in outage_events if e.kind == "true_outage"]

    forced_payments = []
    for payment in payments:
        outage = next(
            (
                e
                for e in true_outages
                if e.issuer_code == payment.issuer_code
                and e.start_time <= payment.failed_at <= e.start_time + timedelta(minutes=e.duration_minutes)
            ),
            None,
        )
        if outage is not None and payment.decline_reason != DeclineReason.ISSUER_DOWN:
            forced_payments.append(replace(payment, decline_reason=DeclineReason.ISSUER_DOWN))
        else:
            forced_payments.append(payment)

    cluster_payments: List[Payment] = []
    for i, event in enumerate(outage_events):
        cluster_payments.extend(
            _generate_cluster_payments(event, customers, f"pay_{event.kind}_{i}", rng)
        )
        if event.kind == "true_outage":
            # Additive front-loaded burst, true_outage only. See
            # _generate_true_outage_burst_payments and DECISIONS.md
            # 2026-08-25. The decoy_cluster branch above is unchanged.
            cluster_payments.extend(
                _generate_true_outage_burst_payments(
                    event, customers, f"pay_{event.kind}_burst_{i}", rng
                )
            )

    return [*forced_payments, *cluster_payments]
