"""Isolation tests for the append-only event log and state-by-replay."""

from datetime import datetime, timedelta, timezone

import pytest

from execution.db import make_engine, make_session_factory
from execution.eventlog import append_event, derive_state, history
from execution.states import AbandonReason, IllegalStateTransition, PaymentState


def _session():
    engine = make_engine("sqlite:///:memory:")
    return make_session_factory(engine)()


def test_state_is_none_with_no_events():
    session = _session()
    assert derive_state(session, "pay_x") is None


def test_state_derived_by_replaying_appended_events():
    session = _session()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    append_event(session, "pay_x", PaymentState.AT_RISK, now)
    append_event(session, "pay_x", PaymentState.DIAGNOSED, now + timedelta(minutes=1))
    append_event(session, "pay_x", PaymentState.SCHEDULED, now + timedelta(minutes=2))
    session.commit()

    assert derive_state(session, "pay_x") == PaymentState.SCHEDULED


def test_append_event_raises_on_illegal_transition():
    session = _session()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    append_event(session, "pay_x", PaymentState.AT_RISK, now)
    session.commit()
    with pytest.raises(IllegalStateTransition):
        append_event(session, "pay_x", PaymentState.RECOVERED, now)  # skips states


def test_append_event_raises_when_reason_missing_on_abandon():
    session = _session()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    append_event(session, "pay_x", PaymentState.AT_RISK, now)
    append_event(session, "pay_x", PaymentState.DIAGNOSED, now)
    session.commit()
    with pytest.raises(IllegalStateTransition):
        append_event(session, "pay_x", PaymentState.ABANDONED, now)


def test_history_returns_full_ordered_sequence_with_reason_recorded():
    session = _session()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    append_event(session, "pay_x", PaymentState.AT_RISK, now)
    append_event(session, "pay_x", PaymentState.DIAGNOSED, now)
    append_event(
        session,
        "pay_x",
        PaymentState.ABANDONED,
        now,
        abandon_reason=AbandonReason.POLICY_STOP,
    )
    session.commit()

    events = history(session, "pay_x")
    assert [e.to_state for e in events] == ["at_risk", "diagnosed", "abandoned"]
    assert events[-1].abandon_reason == "policy_stop"
    assert derive_state(session, "pay_x") == PaymentState.ABANDONED


def test_events_for_different_payments_are_independent():
    session = _session()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    append_event(session, "pay_a", PaymentState.AT_RISK, now)
    append_event(session, "pay_a", PaymentState.DIAGNOSED, now)
    append_event(session, "pay_b", PaymentState.AT_RISK, now)
    session.commit()

    assert derive_state(session, "pay_a") == PaymentState.DIAGNOSED
    assert derive_state(session, "pay_b") == PaymentState.AT_RISK
