"""Isolation tests for the systemic outage detector.

Two kinds of coverage:

1. Deterministic hysteresis mechanics on synthetic data (window/cooldown
   behavior in isolation, independent of the random batch).
2. An empirical threshold study against the canonical batch (seed=42, the
   same batch used throughout this project's DECISIONS.md/harness runs),
   documenting the real precision/recall tradeoff rather than a single
   cherry-picked "it works" setting. See DECISIONS.md 2026-08-25 and
   config.py's DETECTOR_WINDOW_MINUTES comment for the full writeup.

Empirical finding (batch: 300 customers, seed=42, background 450 payments +
outage injection, seed=42): background ISSUER_DOWN rate per issuer over a
15-minute window is ~0.005-0.011 events/window (15-33 events across the full
30-day/2880-window span, per issuer). A literal "5x baseline" threshold is
therefore ~0.03-0.06 -- sub-1, i.e. any single stray failure would "exceed"
it. Practical integer thresholds:

  threshold=2: true_outage lag=34min,  decoy FALSE-POSITIVES (flagged ~17min)
  threshold=3: true_outage lag=64min,  decoy FALSE-POSITIVES (flagged ~17min)
  threshold=5: true_outage lag=69min,  decoy FALSE-POSITIVES (flagged ~15min)
  threshold=6: true_outage lag=70min,  decoy NOT flagged
  threshold=7: true_outage lag=71min,  decoy NOT flagged
  threshold=8: true_outage NEVER flagged (missed), decoy NOT flagged

Why the decoy is so hard to rule out at low thresholds: raising
TRUE_OUTAGE_DURATION_MINUTES from 20->90 (DECISIONS.md, naive-retry-timing
fix) spread the true outage's 15 injected payments across 90 minutes
(density 0.167/min), while the decoy's 6 payments stayed packed into its
original 15-minute window (density 0.4/min) -- the decoy is denser per
minute despite having fewer total events. On this batch, the true outage's
peak 15-min window count (8) narrowly exceeds the decoy's peak (6), which is
what makes threshold=6-7 discriminate at all -- but that margin comes from
this run's specific random clustering, not a structural difference the
detector is reliably exploiting, and even then the lag is over an hour into
a 90-minute outage. There is no count-only threshold that is both fast and
reliably decoy-proof at these cluster sizes; not pretending otherwise.
"""

from datetime import datetime, timedelta

from simulator import (
    generate_customers,
    generate_outage_events,
    generate_payments,
    inject_outage_events,
)
from simulator.outage_detector import OutageDetectorConfig, detect_systemic_event
from simulator.types import DeclineReason, InstrumentType, Payment

WINDOW_START = datetime(2026, 1, 1)
WINDOW_DAYS = 30


def _canonical_batch():
    customers = generate_customers(300, seed=42)
    payments = generate_payments(
        customers, 450, window_start=WINDOW_START, window_days=WINDOW_DAYS, seed=42
    )
    events = generate_outage_events(window_start=WINDOW_START, window_days=WINDOW_DAYS, seed=42)
    payments = inject_outage_events(payments, customers, events, seed=42)
    true_outage = next(e for e in events if e.kind == "true_outage")
    decoy = next(e for e in events if e.kind == "decoy_cluster")
    return payments, true_outage, decoy


def _first_flag_and_any_flag(payments, issuer_code, event_start, cfg, scan_minutes, step_minutes=1):
    first_flag_time = None
    any_flag = False
    t = event_start - timedelta(minutes=10)
    end = event_start + timedelta(minutes=scan_minutes)
    while t <= end:
        if detect_systemic_event(payments, issuer_code, t, cfg):
            any_flag = True
            if first_flag_time is None:
                first_flag_time = t
        t += timedelta(minutes=step_minutes)
    return first_flag_time, any_flag


# --- Deterministic hysteresis mechanics (synthetic data) ---


def _issuer_down_payment(issuer_code: str, failed_at: datetime, i: int) -> Payment:
    return Payment(
        payment_id=f"pay_synth_{i}",
        customer_id="cust_synth",
        amount_paise=100_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=DeclineReason.ISSUER_DOWN,
        issuer_code=issuer_code,
        failed_at=failed_at,
    )


def test_no_failures_never_flags():
    cfg = OutageDetectorConfig(window_minutes=15, count_threshold=3, cooldown_minutes=15)
    assert detect_systemic_event([], "HDFC", datetime(2026, 1, 1), cfg) is False


def test_flags_once_count_exceeds_threshold_within_window():
    base = datetime(2026, 1, 1, 0, 0)
    # 4 failures within a 15-min window for HDFC; threshold=3 -> exceeded.
    failures = [
        _issuer_down_payment("HDFC", base + timedelta(minutes=m), i)
        for i, m in enumerate([0, 2, 4, 6])
    ]
    cfg = OutageDetectorConfig(window_minutes=15, count_threshold=3, cooldown_minutes=15)
    assert detect_systemic_event(failures, "HDFC", base + timedelta(minutes=6), cfg) is True
    # Before the 4th failure, count was only 3 -> not > 3, not flagged yet.
    assert detect_systemic_event(failures, "HDFC", base + timedelta(minutes=5), cfg) is False


def test_other_issuer_is_unaffected():
    base = datetime(2026, 1, 1, 0, 0)
    failures = [
        _issuer_down_payment("HDFC", base + timedelta(minutes=m), i)
        for i, m in enumerate([0, 2, 4, 6])
    ]
    cfg = OutageDetectorConfig(window_minutes=15, count_threshold=3, cooldown_minutes=15)
    assert detect_systemic_event(failures, "ICICI", base + timedelta(minutes=6), cfg) is False


def test_stays_flagged_through_cooldown_after_count_drops():
    base = datetime(2026, 1, 1, 0, 0)
    failures = [
        _issuer_down_payment("HDFC", base + timedelta(minutes=m), i)
        for i, m in enumerate([0, 2, 4, 6])
    ]
    cfg = OutageDetectorConfig(window_minutes=15, count_threshold=3, cooldown_minutes=15)
    # By minute 22, the window (22-15=7..22) no longer contains enough of the
    # original burst to exceed threshold on its own -- but cooldown (15 min
    # from the last breach at minute 6) hasn't elapsed yet, so still flagged.
    assert detect_systemic_event(failures, "HDFC", base + timedelta(minutes=20), cfg) is True


def test_clears_after_cooldown_elapses_with_no_further_breach():
    base = datetime(2026, 1, 1, 0, 0)
    failures = [
        _issuer_down_payment("HDFC", base + timedelta(minutes=m), i)
        for i, m in enumerate([0, 2, 4, 6])
    ]
    cfg = OutageDetectorConfig(window_minutes=15, count_threshold=3, cooldown_minutes=15)
    # Last breach was at minute 6 (count=4>3). Cooldown clears the instant the
    # gap reaches cooldown_minutes (strict "<" in detect_systemic_event, so
    # a gap exactly equal to cooldown already counts as cleared): minute 20
    # (gap=14min) is still within cooldown; minute 21 (gap=15min) clears.
    assert detect_systemic_event(failures, "HDFC", base + timedelta(minutes=20), cfg) is True
    assert detect_systemic_event(failures, "HDFC", base + timedelta(minutes=21), cfg) is False


def test_a_second_later_burst_re_flags_after_clearing():
    base = datetime(2026, 1, 1, 0, 0)
    first_burst = [
        _issuer_down_payment("HDFC", base + timedelta(minutes=m), i)
        for i, m in enumerate([0, 2, 4, 6])
    ]
    second_burst = [
        _issuer_down_payment("HDFC", base + timedelta(hours=1, minutes=m), i + 10)
        for i, m in enumerate([0, 2, 4, 6])
    ]
    failures = first_burst + second_burst
    cfg = OutageDetectorConfig(window_minutes=15, count_threshold=3, cooldown_minutes=15)
    assert detect_systemic_event(failures, "HDFC", base + timedelta(minutes=30), cfg) is False
    assert (
        detect_systemic_event(failures, "HDFC", base + timedelta(hours=1, minutes=6), cfg) is True
    )


# --- Empirical threshold study on the canonical batch (seed=42) ---


def test_background_baseline_rate_is_far_below_one_event_per_window():
    """Documents WHY a literal '5x baseline' threshold isn't directly usable:
    the background per-issuer rate is sub-1 event per 15-min window, so 5x it
    is also sub-1."""
    from collections import Counter

    customers = generate_customers(300, seed=42)
    background_payments = generate_payments(
        customers, 450, window_start=WINDOW_START, window_days=WINDOW_DAYS, seed=42
    )
    counts = Counter(
        p.issuer_code for p in background_payments if p.decline_reason == DeclineReason.ISSUER_DOWN
    )
    n_windows = (WINDOW_DAYS * 24 * 60) / 15
    for issuer, count in counts.items():
        baseline_rate = count / n_windows
        assert 0 < baseline_rate < 0.02, f"{issuer}: {baseline_rate}"
        assert 5 * baseline_rate < 1, f"5x baseline for {issuer} should be sub-1"


def test_low_thresholds_detect_true_outage_but_also_false_positive_on_decoy():
    payments, true_outage, decoy = _canonical_batch()
    for threshold, expected_lag_minutes in [(2, 34), (3, 64), (5, 69)]:
        cfg = OutageDetectorConfig(window_minutes=15, count_threshold=threshold, cooldown_minutes=15)

        first_flag, _ = _first_flag_and_any_flag(
            payments, true_outage.issuer_code, true_outage.start_time, cfg,
            scan_minutes=true_outage.duration_minutes + 60,
        )
        assert first_flag is not None, f"threshold={threshold}: true_outage should be detected"
        lag_minutes = (first_flag - true_outage.start_time).total_seconds() / 60
        assert lag_minutes == expected_lag_minutes, f"threshold={threshold}: lag={lag_minutes}"

        _, decoy_ever_flagged = _first_flag_and_any_flag(
            payments, decoy.issuer_code, decoy.start_time, cfg, scan_minutes=decoy.duration_minutes + 30
        )
        assert decoy_ever_flagged is True, (
            f"threshold={threshold}: decoy is expected to false-positive at this "
            f"threshold on this batch -- honestly documenting the limitation, "
            f"not asserting it away"
        )


def test_threshold_six_or_seven_discriminates_on_this_batch_but_the_margin_is_narrow():
    """At 6-7, the true outage's random peak (8) narrowly beats the decoy's
    peak (6) on this specific seed -- real discrimination, but a fragile
    margin from clustering variance, not a robust structural gap. Detection
    lag here is over an hour into a 90-minute outage."""
    payments, true_outage, decoy = _canonical_batch()
    for threshold in (6, 7):
        cfg = OutageDetectorConfig(window_minutes=15, count_threshold=threshold, cooldown_minutes=15)

        first_flag, _ = _first_flag_and_any_flag(
            payments, true_outage.issuer_code, true_outage.start_time, cfg,
            scan_minutes=true_outage.duration_minutes + 60,
        )
        assert first_flag is not None
        lag_minutes = (first_flag - true_outage.start_time).total_seconds() / 60
        assert lag_minutes > 60  # over an hour -- not fast

        _, decoy_ever_flagged = _first_flag_and_any_flag(
            payments, decoy.issuer_code, decoy.start_time, cfg, scan_minutes=decoy.duration_minutes + 30
        )
        assert decoy_ever_flagged is False


def test_threshold_eight_misses_the_true_outage_entirely():
    """One count higher than the true outage's own peak (8) -> it's never
    detected at all. This is the other edge of the tradeoff: push threshold
    up to kill the decoy false-positive and you can lose real detection too."""
    payments, true_outage, decoy = _canonical_batch()
    cfg = OutageDetectorConfig(window_minutes=15, count_threshold=8, cooldown_minutes=15)
    first_flag, _ = _first_flag_and_any_flag(
        payments, true_outage.issuer_code, true_outage.start_time, cfg,
        scan_minutes=true_outage.duration_minutes + 60,
    )
    assert first_flag is None
