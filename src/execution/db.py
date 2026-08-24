"""Shared SQLAlchemy setup for the execution layer.

One Base, one engine-factory. Callers (including tests) create their own
engine/session so a test can point at ":memory:" and get full isolation
without touching a real file. Nothing in this module is a singleton
connection to a real DB — that's assembled by whoever calls make_engine().
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def make_engine(url: str = "sqlite:///execution.db"):
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            # Without StaticPool, each new connection to sqlite:///:memory:
            # (e.g. one opened from a different thread -- which FastAPI's
            # BackgroundTasks can do) gets its own separate, empty in-memory
            # database. StaticPool forces every checkout through the same
            # single connection so :memory: actually behaves like a shared DB.
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
