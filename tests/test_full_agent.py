"""Isolation tests for full_agent's decide() -- compliance vetoes fire
BEFORE scoring, the systemic detector changes ISSUER_DOWN's decision, and
STOP is genuinely reachable, not a fallback bolted on afterward."""

from datetime import datetime, timedelta

from simulator import ContactTracker, DeclineReason, InstrumentType, Language
from simulator.full_agent import decide
from simulator.outage_detector import OutageDetectorConfig
from simulator.types import ActionType, Customer, OutageEvent, Payment

WINDOW_START = datetime(2026, 1, 1)


def _persona(**overrides):
    base = dict(
        customer_id="cust_x",
        recovery_propensity=0.6,
        funds_arrival_day=2,
        contact_hours=(9, 21),
        annoyance_threshold=10,
        channel_response_rate={"upi_link": 0.7, "whatsapp": 0.6, "sms": 0.5},
        preferred_language=Language.EN,
    )
    base.update(overrides)
    return Customer(**base)


# 10am -- comfortably inside the default (9, 21) contact_hours even after a
# candidate's typical offset (up to a few hours) is added.
DEFAULT_FAILED_AT = datetime(2026, 1, 1, 10, 0)


def _payment(decline_reason, issuer_code="HDFC", failed_at=DEFAULT_FAILED_AT, amount_paise=500_000):
    return Payment(
        payment_id="pay_x",
        customer_id="cust_x",
        amount_paise=amount_paise,
        instrument_type=InstrumentType.CARD,
        decline_reason=decline_reason,
        issuer_code=issuer_code,
        failed_at=failed_at,
    )


def test_insufficient_funds_picks_send_payment_link():
    decision = decide(
        _payment(DeclineReason.INSUFFICIENT_FUNDS), _persona(), WINDOW_START, ContactTracker(), [], []
    )
    assert decision is not None
    offset, action = decision
    assert action.action_type == ActionType.SEND_PAYMENT_LINK


def test_mandate_revoked_has_no_candidates_stop_wins():
    decision = decide(
        _payment(DeclineReason.MANDATE_REVOKED), _persona(), WINDOW_START, ContactTracker(), [], []
    )
    assert decision is None


def test_contact_hours_veto_forces_stop_even_though_action_would_otherwise_score_positive():
    """A compliance rule that can be outscored isn't a compliance rule: a
    huge payment amount must NOT buy its way past the contact-hours veto."""
    persona = _persona(contact_hours=(9, 10))  # a narrow window
    # AFA_3DS_DROPOFF's candidate fires at +30min; failed_at at midnight
    # puts the action_time well outside (9,10).
    payment = _payment(DeclineReason.AFA_3DS_DROPOFF, amount_paise=5_000_000)  # huge amount
    decision = decide(payment, persona, WINDOW_START, ContactTracker(), [], [])
    assert decision is None  # vetoed, not scored around


def test_utc_action_time_outside_ist_window_numerically_is_still_permitted_once_converted():
    """P1 timezone fix (2026-08-25, DECISIONS.md): the contact-hours veto
    must evaluate the customer's declared window in IST, not against
    whatever zone action_time happens to be expressed in. This is the case
    that would have been WRONGLY VETOED by the pre-fix bug (a bare
    action_time.hour read): 04:00 UTC is outside (9, 21) numerically, but
    converts to 09:30 IST -- inside the window, so the real-world contact
    is genuinely allowed and must not be blocked by a clock error."""
    from zoneinfo import ZoneInfo

    persona = _persona(contact_hours=(9, 21))
    utc_time = datetime(2026, 1, 1, 4, 0, tzinfo=ZoneInfo("UTC"))
    assert not (9 <= utc_time.hour < 21)  # the bug's bare .hour read would reject this
    payment = _payment(DeclineReason.RISK_DECLINE, failed_at=utc_time)

    decision = decide(payment, persona, WINDOW_START, ContactTracker(), [], [])

    assert decision is not None
    assert decision[1].action_type == ActionType.ESCALATE_ALTERNATE_INSTRUMENT


def test_utc_action_time_inside_ist_window_numerically_is_correctly_vetoed():
    """The other direction, and the more dangerous one: this is the case
    that would have been WRONGLY PERMITTED by the pre-fix bug. 16:00 UTC is
    inside (9, 21) numerically -- a bare action_time.hour read would have
    let this contact through -- but it converts to 21:30 IST, outside the
    customer's declared window. On a UTC-hosted deployment (the only
    realistic one) the pre-fix code would have contacted this customer
    outside their allowed hours and never known it."""
    from zoneinfo import ZoneInfo

    persona = _persona(contact_hours=(9, 21))
    utc_time = datetime(2026, 1, 1, 16, 0, tzinfo=ZoneInfo("UTC"))
    assert 9 <= utc_time.hour < 21  # the bug's bare .hour read would (wrongly) accept this
    payment = _payment(DeclineReason.RISK_DECLINE, failed_at=utc_time, amount_paise=5_000_000)

    decision = decide(payment, persona, WINDOW_START, ContactTracker(), [], [])

    assert decision is None  # vetoed: true IST hour is 21, outside the window


def test_weekly_cap_veto_stops_further_customer_facing_contact():
    from simulator import config

    persona = _persona()
    tracker = ContactTracker()
    for _ in range(config.MAX_WEEKLY_CONTACTS):
        tracker._counts[persona.customer_id] = tracker.contact_count(persona.customer_id) + 1

    payment = _payment(DeclineReason.AFA_3DS_DROPOFF)  # customer-facing (send_nudge)
    decision = decide(payment, persona, WINDOW_START, tracker, [], [])
    assert decision is None


def test_silent_action_is_not_subject_to_contact_hours_or_weekly_cap():
    from simulator import config

    persona = _persona(contact_hours=(9, 10))  # narrow window
    tracker = ContactTracker()
    for _ in range(config.MAX_WEEKLY_CONTACTS + 5):
        tracker._counts[persona.customer_id] = tracker.contact_count(persona.customer_id) + 1

    # NETWORK_TIMEOUT -> retry_now, a silent action -- unaffected by either veto.
    payment = _payment(DeclineReason.NETWORK_TIMEOUT)
    decision = decide(payment, persona, WINDOW_START, tracker, [], [])
    assert decision is not None
    assert decision[1].action_type == ActionType.RETRY_NOW


def test_issuer_down_holds_when_outage_detected_instead_of_retrying_into_it():
    persona = _persona()
    payment = _payment(DeclineReason.ISSUER_DOWN, issuer_code="HDFC")
    detector_cfg = OutageDetectorConfig(window_minutes=15, count_threshold=3, cooldown_minutes=15)

    # A dense burst on the same issuer, landing inside the trailing 15-min
    # window ending at the +20min prospective retry time (i.e. within
    # (20-15, 20] = (5, 20] minutes after failure) -- enough to exceed
    # count_threshold=3. Anchored to the PAYMENT's own failed_at, not the
    # unrelated module-level WINDOW_START.
    burst = [
        Payment(
            payment_id=f"pay_burst_{i}",
            customer_id="cust_other",
            amount_paise=100_000,
            instrument_type=InstrumentType.CARD,
            decline_reason=DeclineReason.ISSUER_DOWN,
            issuer_code="HDFC",
            failed_at=payment.failed_at + timedelta(minutes=m),
        )
        for i, m in enumerate([10, 12, 14, 16, 18])
    ]

    with_detection = decide(
        payment, persona, WINDOW_START, ContactTracker(), burst, [], detector_config=detector_cfg,
        disable_outage_detection=False,
    )
    ablated = decide(
        payment, persona, WINDOW_START, ContactTracker(), burst, [], detector_config=detector_cfg,
        disable_outage_detection=True,
    )

    assert with_detection is not None and ablated is not None
    detected_offset, _ = with_detection
    ablated_offset, _ = ablated
    # Outage detected -> holds for a much longer offset than the ablation,
    # which always uses the naive ~20min retry regardless of the burst.
    assert detected_offset > ablated_offset
    assert ablated_offset == timedelta(minutes=20)


def test_issuer_down_no_outage_detected_uses_the_short_retry_offset():
    persona = _persona()
    payment = _payment(DeclineReason.ISSUER_DOWN, issuer_code="HDFC")
    decision = decide(payment, persona, WINDOW_START, ContactTracker(), [], [])
    assert decision is not None
    offset, action = decision
    assert offset == timedelta(minutes=20)
    assert action.action_type == ActionType.RETRY_SCHEDULED


def test_explicit_opt_out_vetoes_regardless_of_weekly_cap_or_contact_hours():
    """DECISIONS.md 2026-08-25: explicit_opt_out is its own hard veto, not
    folded into the weekly-cap check -- a customer who's never been
    contacted (contact_count=0, well inside their allowed hours) but HAS
    explicitly opted out must still be vetoed."""
    persona = _persona()
    tracker = ContactTracker()
    tracker.mark_opted_out(persona.customer_id, DEFAULT_FAILED_AT)  # before any action

    payment = _payment(DeclineReason.AFA_3DS_DROPOFF)  # customer-facing (send_nudge)
    decision = decide(payment, persona, WINDOW_START, tracker, [], [])
    assert decision is None


def test_explicit_opt_out_does_not_retroactively_veto_actions_scheduled_before_it():
    """as_of semantics: an opt-out recorded AFTER a candidate's action_time
    doesn't veto that candidate -- the customer hadn't said stop yet."""
    persona = _persona()
    tracker = ContactTracker()
    payment = _payment(DeclineReason.AFA_3DS_DROPOFF)  # candidate fires at failed_at+30min
    opted_out_after_the_candidates_action_time = payment.failed_at + timedelta(hours=5)
    tracker.mark_opted_out(persona.customer_id, opted_out_after_the_candidates_action_time)

    decision = decide(payment, persona, WINDOW_START, tracker, [], [])
    assert decision is not None


def test_explicit_opt_out_is_reported_as_a_distinct_reason_from_weekly_cap():
    """The two hard-cap-shaped vetoes must be independently identifiable,
    not merged into one ambiguous reason string."""
    from simulator.full_agent import Candidate, _veto_reason
    from simulator.types import Action

    persona = _persona()
    action_time = DEFAULT_FAILED_AT
    candidate = Candidate(timedelta(0), Action(ActionType.SEND_NUDGE), 0.4)

    opted_out_tracker = ContactTracker()
    opted_out_tracker.mark_opted_out(persona.customer_id, action_time)
    assert _veto_reason(candidate, persona, action_time, opted_out_tracker) == "explicit_opt_out"

    from simulator import config

    capped_tracker = ContactTracker()
    for _ in range(config.MAX_WEEKLY_CONTACTS):
        capped_tracker._counts[persona.customer_id] = capped_tracker.contact_count(persona.customer_id) + 1
    assert _veto_reason(candidate, persona, action_time, capped_tracker) == "weekly_cap"


def test_tiny_payment_amount_can_make_stop_win_over_a_customer_facing_action():
    """expected_value = rate * amount - cost; when amount is small enough
    that cost dominates, STOP (score 0) beats every real candidate."""
    persona = _persona()
    # AFA_3DS_DROPOFF customer-facing rate 0.40; at amount_paise=1 the
    # expected value is ~0.4 - 200 << 0, well under STOP's 0.
    payment = _payment(DeclineReason.AFA_3DS_DROPOFF, amount_paise=1)
    decision = decide(payment, persona, WINDOW_START, ContactTracker(), [], [])
    assert decision is None
