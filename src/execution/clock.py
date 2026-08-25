"""Timezone discipline for the execution layer -- the P1 fix.

Two real bugs, same root cause (2026-08-25, DECISIONS.md): webhook.py
called bare `datetime.now()` -- host-local time, not guaranteed to be IST
or even UTC -- and full_agent.py's contact-hours veto compared an hour
straight off whatever clock produced the timestamp. On a UTC-hosted
deployment (the only realistic one), the guardrail was silently evaluating
against the wrong clock, off by 5 hours 30 minutes. A guardrail that's off
by 5.5 hours isn't a guardrail.

The fix has two halves. This module owns the execution-layer half: every
timestamp execution/ writes or reads is timezone-aware UTC, never a bare
host-local read, and that's enforced at the storage boundary, not by
convention. simulator/timezones.py owns the other half -- converting a
UTC-aware execution-layer timestamp to IST before evaluating an
hour-of-day business rule against it.

utc_now() is the one place execution/ is allowed to read the real clock
from (webhook.py's receive/process handlers). Everywhere else, `now` is
threaded in as an explicit parameter, same discipline decide() and the
outbox/reconciliation workers already followed before this fix -- this
doesn't change that, it just makes the one remaining real-clock read
explicit and correct instead of implicit and host-dependent.

UTCDateTime is why `timezone=True` alone isn't enough on SQLite: it was
checked, not assumed, that a plain `DateTime(timezone=True)` column round-
trips a timezone-aware value correctly on WRITE but hands back a NAIVE
datetime on READ regardless (tzinfo silently dropped) -- SQLite has no
native tz-aware timestamp type, and SQLAlchemy's default DATETIME adapter
for it doesn't restore one. Relying on the column flag alone would have
quietly reintroduced naive datetimes at every read site, defeating the
entire fix. UTCDateTime is a TypeDecorator that owns both directions
explicitly: process_bind_param REJECTS a naive datetime outright (this IS
the storage-boundary assertion -- one mechanism, applied automatically to
every column that uses this type, rather than a check scattered at each
write call site) and normalizes to UTC before handing to SQLite;
process_result_value reattaches tzinfo=UTC on the way back out, so a value
read through this type is always aware, regardless of what SQLite itself
preserved.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


def utc_now() -> datetime:
    """The one real-clock read allowed in execution/."""
    return datetime.now(timezone.utc)


class NaiveDatetimeError(ValueError):
    pass


class UTCDateTime(TypeDecorator):
    """A DateTime column that stores UTC and refuses a naive datetime at
    the storage boundary. See module docstring for why a plain
    DateTime(timezone=True) isn't sufficient on SQLite."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise NaiveDatetimeError(
                f"refusing to store a naive datetime ({value!r}) -- execution/ "
                "requires timezone-aware UTC at the storage boundary. Construct "
                "it with tzinfo=timezone.utc (or execution.clock.utc_now())."
            )
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)
