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


# --- The 3-seed harness re-run (2026-08-25 DECISIONS.md) showed full_agent's
# Rs/contact dropping ~4% after this fix -- a "premature probe" (the
# breaker read CLOSED from the failure log, but the TRUE simulator outage
# was still live) is a permanent loss under this harness's one-shot
# decide()-per-payment architecture, since there's no retry-on-failure
# loop to give it a second chance. Explicitly asked to substantiate that
# claim rather than assert it, not tune the checkpoints to hide it. Two
# things below: a reference to the test above as evidence the BREAKER
# ITSELF correctly treats a still-active outage as "probe failed, don't
# widen" (test_if_probes_fail_the_breaker_reopens_rather_than_widening),
# and a direct empirical count of how much of that 4% is actually
# recoverable in principle -- i.e. how much is a harness artifact
# specifically, not an unavoidable cost of the breaker's own design.


def test_a_premature_probe_is_recoverable_at_the_next_checkpoint_confirming_the_harness_is_the_limitation():
    """Direct empirical count, run against the SAME real batch-generation
    pipeline (not a hand-built scenario) used for the 3-seed harness
    comparison: of the held ISSUER_DOWN payments whose early
    probe/ramp checkpoint landed while the TRUE outage (ground truth, not
    the detector's belief) was still live -- the "wasted probe" cohort --
    check whether the conservative FALLBACK checkpoint (the same one a
    real system's next retry cycle would land on, after the breaker
    correctly reopens per the test above) would have landed after the
    true outage actually ended.

    If ~all of them would have, that bounds the 4% harness-measured cost
    as ALMOST ENTIRELY a single-shot-architecture artifact, not a real,
    unavoidable cost of probing early -- substantiating the DECISIONS.md
    claim with a number, not just an assertion. Confirmed across all 3
    of the harness's own seeds (42, 7, 123) before relying on this
    single-seed version: 9/9, 8/8, 9/9 recoverable respectively -- this
    test locks in seed 42's count specifically as a permanent regression
    guard against silently drifting away from that finding."""
    from simulator import generate_customers, generate_outage_events, generate_payments, inject_outage_events
    from simulator.full_agent import (
        ASSUMED_MAX_OUTAGE_DURATION_MINUTES,
        DEFAULT_DETECTOR_CONFIG,
        _issuer_down_release_offset,
    )
    from simulator.outage_detector import detect_systemic_event

    # DEFAULT_DETECTOR_CONFIG -- the production config decide() actually
    # uses by default, and what the 3-seed harness comparison ran with.
    # NOT this file's own DETECTOR_CFG (a lower, more sensitive threshold
    # used deliberately by the other tests above for small hand-built
    # bursts) -- using the wrong one here silently changes which payments
    # count as "held" and would misreport this finding.
    seed = 42
    generation_window_start = datetime(2026, 1, 1)
    customers = generate_customers(300, seed=seed)
    payments = generate_payments(customers, window_start=generation_window_start, window_days=30, seed=seed)
    events = generate_outage_events(window_start=generation_window_start, window_days=30, seed=seed)
    payments = inject_outage_events(payments, customers, events, seed=seed)
    true_outages = [e for e in events if e.kind == "true_outage"]

    def true_outage_for(payment):
        return next((e for e in true_outages if e.issuer_code == payment.issuer_code), None)

    issuer_down = [p for p in payments if p.decline_reason == DeclineReason.ISSUER_DOWN]
    held = [
        p
        for p in issuer_down
        if detect_systemic_event(
            payments, p.issuer_code, p.failed_at + timedelta(minutes=20), DEFAULT_DETECTOR_CONFIG
        )
    ]
    assert len(held) == 25, "sanity check against the diagnosed batch -- fail loudly if generation drifted"

    fallback_offset = timedelta(
        minutes=DEFAULT_DETECTOR_CONFIG.window_minutes
        + DEFAULT_DETECTOR_CONFIG.cooldown_minutes
        + ASSUMED_MAX_OUTAGE_DURATION_MINUTES
    )

    wasted = []
    for p in held:
        offset = _issuer_down_release_offset(p, payments, DEFAULT_DETECTOR_CONFIG, attempt_number=1)
        release_time = p.failed_at + offset
        outage = true_outage_for(p)
        in_true_outage = outage and outage.start_time <= release_time <= outage.start_time + timedelta(
            minutes=outage.duration_minutes
        )
        if offset < fallback_offset and in_true_outage:
            wasted.append(p)

    recoverable_at_fallback = 0
    for p in wasted:
        outage = true_outage_for(p)
        fallback_time = p.failed_at + fallback_offset
        outage_end = outage.start_time + timedelta(minutes=outage.duration_minutes)
        if fallback_time > outage_end:
            recoverable_at_fallback += 1

    assert len(wasted) == 9, "sanity check against the diagnosed batch -- fail loudly if generation drifted"
    assert recoverable_at_fallback == len(wasted), (
        f"only {recoverable_at_fallback}/{len(wasted)} wasted probes would have been recoverable at the "
        "fallback checkpoint -- the harness-artifact claim in DECISIONS.md needs revisiting if this drops"
    )
