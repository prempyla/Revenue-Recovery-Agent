"""P1 timezone fix (2026-08-25, DECISIONS.md): execution/'s storage
boundary must reject a naive datetime outright rather than silently
accepting host-local time, so the original bug (webhook.py's bare
datetime.now()) can't silently regress. UTCDateTime is the single
mechanism this is enforced through -- applied automatically to every
column that uses it, not a check scattered at each write call site."""

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import StatementError

from execution.clock import NaiveDatetimeError
from execution.db import make_engine, make_session_factory
from execution.eventlog import append_event
from execution.states import PaymentState


def _session():
    engine = make_engine("sqlite:///:memory:")
    return make_session_factory(engine)()


def _assert_raises_naive_datetime_error(callable_):
    """process_bind_param's raise happens inside SQLAlchemy's own statement
    execution, so it arrives wrapped in a StatementError rather than as a
    bare NaiveDatetimeError -- check the wrapped cause, not just that
    *something* raised."""
    with pytest.raises(StatementError) as exc_info:
        callable_()
    assert isinstance(exc_info.value.orig, NaiveDatetimeError)


def test_naive_datetime_at_the_storage_boundary_raises():
    session = _session()
    naive = datetime(2026, 1, 1, 10, 0)  # no tzinfo
    append_event(session, "pay_x", PaymentState.AT_RISK, naive)
    _assert_raises_naive_datetime_error(session.commit)


def test_naive_datetime_raises_regardless_of_which_column_it_targets():
    """Not a special case for EventRecord specifically -- OutboxIntent's
    due_at/created_at go through the same UTCDateTime type."""
    from execution.idempotency import make_idempotency_key
    from execution.outbox import write_intent_with_state_change

    session = _session()
    append_event(session, "pay_x", PaymentState.AT_RISK, datetime(2026, 1, 1, tzinfo=timezone.utc))
    append_event(session, "pay_x", PaymentState.DIAGNOSED, datetime(2026, 1, 1, tzinfo=timezone.utc))
    session.commit()

    naive = datetime(2026, 1, 1, 11, 0)  # no tzinfo
    _assert_raises_naive_datetime_error(
        lambda: write_intent_with_state_change(
            session,
            "pay_x",
            make_idempotency_key("pay_x", 1),
            "send_payment_link",
            {"amount_paise": 100_000, "customer_name": "C", "customer_contact": "9000000000"},
            naive,
        )
    )


def test_timezone_aware_datetime_is_accepted_and_reads_back_aware():
    session = _session()
    aware = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    append_event(session, "pay_x", PaymentState.AT_RISK, aware)
    session.commit()

    from execution.eventlog import history

    row = history(session, "pay_x")[0]
    assert row.event_time == aware
    assert row.event_time.tzinfo is not None


def test_non_utc_aware_datetime_is_accepted_and_normalized_to_utc_on_read():
    """A tz-aware datetime in a different zone is legal -- it isn't naive --
    and must be normalized, not stored with its original offset silently
    misinterpreted as UTC."""
    from zoneinfo import ZoneInfo

    session = _session()
    ist_time = datetime(2026, 1, 1, 15, 30, tzinfo=ZoneInfo("Asia/Kolkata"))  # == 10:00 UTC
    append_event(session, "pay_x", PaymentState.AT_RISK, ist_time)
    session.commit()

    from execution.eventlog import history

    row = history(session, "pay_x")[0]
    assert row.event_time == datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
