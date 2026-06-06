"""SQLAlchemy engine and session factory.

Postgres-only. ``DATABASE_URL`` must be a postgresql+psycopg URL — pick that
up from .env. Pool defaults are tuned for a single-FastAPI-process backend;
revisit if you scale to multiple workers or add long-lived background jobs.
"""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from config import get_settings

settings = get_settings()

engine = create_engine(
    settings.database_url,
    # pool_pre_ping issues a cheap SELECT 1 before handing out a connection so
    # we don't return a connection that the DB or a proxy has silently dropped.
    # Cheap insurance against transient connection issues; recommended for any
    # Postgres deployment behind a load balancer or with idle-timeout proxies.
    pool_pre_ping=True,
    # Conservative pool sizes for a single web process. Bump pool_size up if
    # you see "QueuePool limit overflow" errors; bump max_overflow if you have
    # bursty traffic patterns.
    pool_size=10,
    max_overflow=20,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a session that auto-closes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()