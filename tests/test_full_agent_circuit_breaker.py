"""P1 #3 (2026-08-25, DECISIONS.md): jittered, ramped outage resume.

Before this, every payment held for a detected ISSUER_DOWN outage was
scheduled at the exact same fixed hold_offset -- a thundering herd the
instant an outage cleared. Two fixes: deterministic jitter (bounded
randomness derived from (payment_id, attempt_number), not Python's
`random`, so decide() stays pure and replayable -- same reasoning as
idempotency.make_idempotency_key), and a named circuit breaker
(closed/open/half_open) gating how many held payments get an early
release opportunity vs. falling through to the original conservative
fallback.
"""

from datetime import datetime, timedelta

from simulator import ContactTracker, DeclineReason, InstrumentType, Language
from simulator.full_agent import (
    CIRCUIT_BREAKER_WINDOW_MINUTES,
    CircuitBreakerState,
    _deterministic_jitter,
    circuit_breaker_state,
    decide,
)
from simulator.outage_detector import OutageDetectorConfig
from simulator.types import Customer, Payment

WINDOW_START = datetime(2026, 1, 1, 10, 0)

DETECTOR_CFG = OutageDetectorConfig(window_minutes=15, count_threshold=3, cooldown_minutes=15)
HYSTERESIS_CLEAR_MINUTES = DETECTOR_CFG.window_minutes + DETECTOR_CFG.cooldown_minutes  # 30


def _persona(**overrides):
    base = dict(
        customer_id="cust_cb",
        recovery_propensity=0.6,
        funds_arrival_day=2,
        contact_hours=(0, 24),
        annoyance_threshold=999,
        channel_response_rate={"upi_link": 0.7, "whatsapp": 0.6, "sms": 0.5},
        preferred_language=Language.EN,
    )
    base.update(overrides)
    return Customer(**base)


def _issuer_down_payment(payment_id, failed_at=WINDOW_START, issuer_code="HDFC", customer_id="cust_cb"):
    return Payment(
        payment_id=payment_id,
        customer_id=customer_id,
        amount_paise=500_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=DeclineReason.ISSUER_DOWN,
        issuer_code=issuer_code,
        failed_at=failed_at,
    )


def _burst(issuer_code, minutes, customer_id="cust_other"):
    """ISSUER_DOWN failures for OTHER customers, at the given offsets from
    WINDOW_START -- enough on their own to trigger detect_systemic_event
    when they fall inside its trailing window."""
    return [
        _issuer_down_payment(f"pay_burst_{m}", WINDOW_START + timedelta(minutes=m), issuer_code, customer_id)
        for m in minutes
    ]


# detect_systemic_event trips on count > count_threshold (strictly), so a
# threshold of 3 needs 4 failures within the trailing window to breach it.
#
# A burst that trips the near-term (+20min) check but has fully aged out
# of the detector's window by the probe checkpoint (+30min) and stays
# clear from then on -- "the outage genuinely cleared" scenario.
CLEARING_BURST = _burst("HDFC", [7, 9, 11, 13])

# A burst that keeps failing well past both the probe and ramp
# checkpoints -- "probes keep failing" scenario.
PERSISTING_BURST = _burst("HDFC", [7, 9, 11, 13, 21, 23, 25, 27, 36, 38, 40, 42])


def _held_offsets(payment_ids, failure_log, detector_config=DETECTOR_CFG):
    persona = _persona()
    offsets = []
    for pid in payment_ids:
        payment = _issuer_down_payment(pid)
        decision = decide(
            payment, persona, WINDOW_START, ContactTracker(), failure_log, [], detector_config=detector_config
        )
        assert decision is not None, f"{pid}: expected a hold/retry candidate, got STOP"
        offsets.append(decision[0])
    return offsets


# --- circuit_breaker_state itself ---


def test_circuit_breaker_is_open_while_the_outage_is_active():
    at = WINDOW_START + timedelta(minutes=14)
    assert circuit_breaker_state(CLEARING_BURST, "HDFC", at, DETECTOR_CFG) == CircuitBreakerState.OPEN


def test_circuit_breaker_is_half_open_right_after_it_clears():
    at = WINDOW_START + timedelta(minutes=HYSTERESIS_CLEAR_MINUTES + 1)
    assert circuit_breaker_state(CLEARING_BURST, "HDFC", at, DETECTOR_CFG) == CircuitBreakerState.HALF_OPEN


def test_circuit_breaker_is_closed_once_confirmed_clear_for_a_full_window():
    at = WINDOW_START + timedelta(minutes=HYSTERESIS_CLEAR_MINUTES + CIRCUIT_BREAKER_WINDOW_MINUTES + 1)
    assert circuit_breaker_state(CLEARING_BURST, "HDFC", at, DETECTOR_CFG) == CircuitBreakerState.CLOSED


# --- jitter ---


def test_deterministic_jitter_is_reproducible_across_separate_calls():
    a = _deterministic_jitter("pay_x", 1, timedelta(minutes=15))
    b = _deterministic_jitter("pay_x", 1, timedelta(minutes=15))
    assert a == b


def test_deterministic_jitter_differs_across_different_payment_ids():
    values = {_deterministic_jitter(f"pay_{i}", 1, timedelta(minutes=15)) for i in range(20)}
    assert len(values) > 1  # not all colliding on the same offset


def test_n_payments_held_through_one_outage_do_not_all_schedule_to_the_same_instant():
    """The actual bug this fixes: before jitter, every held payment got
    the SAME hold_offset. With jitter, N different payment_ids must not
    all resolve to the same release time."""
    payment_ids = [f"pay_{i:03d}" for i in range(20)]
    offsets = _held_offsets(payment_ids, PERSISTING_BURST)  # persisting burst -> all fall to fallback+jitter
    assert len(set(offsets)) > 1, "jitter should have spread these out"


# --- ramped release: bounded probe count, reopen on failure, widen on success ---


def test_on_outage_clear_only_a_bounded_number_release_in_the_first_window():
    """half_open: with the outage genuinely cleared by the probe
    checkpoint, only a small deterministic FRACTION of held payments get
    the earliest release offset -- not all of them."""
    payment_ids = [f"pay_{i:03d}" for i in range(100)]
    offsets = _held_offsets(payment_ids, CLEARING_BURST)

    probe_ceiling = WINDOW_START + timedelta(minutes=HYSTERESIS_CLEAR_MINUTES) - WINDOW_START + timedelta(minutes=16)
    released_in_first_window = sum(1 for o in offsets if o < timedelta(minutes=HYSTERESIS_CLEAR_MINUTES + 16))
    assert 0 < released_in_first_window < len(payment_ids), (
        f"{released_in_first_window}/{len(payment_ids)} released in the first window -- "
        "expected a bounded fraction, not none and not all"
    )
    # Roughly CIRCUIT_BREAKER_PROBE_FRACTION (~10%), generous tolerance for hash variance.
    assert released_in_first_window < len(payment_ids) * 0.3


def test_if_probes_fail_the_breaker_reopens_rather_than_widening():
    """A burst that keeps tripping the detector well past BOTH the probe
    and ramp checkpoints: every held payment must fall through to the
    conservative fallback -- none released early, regardless of their
    ramp bucket."""
    payment_ids = [f"pay_{i:03d}" for i in range(30)]
    offsets = _held_offsets(payment_ids, PERSISTING_BURST)

    fallback_floor = timedelta(minutes=HYSTERESIS_CLEAR_MINUTES + 100)  # ASSUMED_MAX_OUTAGE_DURATION_MINUTES
    assert all(o >= fallback_floor for o in offsets), "some payment released early despite the outage still failing"


def test_if_probes_succeed_the_gate_widens_over_subsequent_windows():
    """With the outage genuinely cleared, the SECOND checkpoint (ramp)
    must admit a strictly larger cumulative slice of held payments than
    the FIRST (probe) -- the gate widening, not staying flat."""
    payment_ids = [f"pay_{i:03d}" for i in range(200)]
    offsets = _held_offsets(payment_ids, CLEARING_BURST)

    probe_boundary = timedelta(minutes=HYSTERESIS_CLEAR_MINUTES + 16)  # probe offset + jitter ceiling
    ramp_boundary = timedelta(minutes=HYSTERESIS_CLEAR_MINUTES + CIRCUIT_BREAKER_WINDOW_MINUTES + 16)

    released_by_probe_window = sum(1 for o in offsets if o < probe_boundary)
    released_by_ramp_window = sum(1 for o in offsets if o < ramp_boundary)

    assert released_by_ramp_window > released_by_probe_window, (
        "the ramp window should admit strictly more than the probe window alone"
    )
