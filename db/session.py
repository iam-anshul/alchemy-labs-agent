"""SQLAlchemy engine and session factory.

Reads ``DATABASE_URL`` from settings.  SQLite gets ``check_same_thread=False``
for compatibility with FastAPI's async context.
"""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from config import get_settings

settings = get_settings()

connect_args: dict[str, object] = {}
if settings.database_url.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a session that auto-closes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
