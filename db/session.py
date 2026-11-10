"""
Database engine and session management.

Loads `.env` on import via `python-dotenv` so Docker Compose, Alembic, and
SQLAlchemy all read the same source of truth. There is no hardcoded
fallback for the test connection: the reviewer's correction patch
eliminated it so tests cannot accidentally target the development
database.

Design choices:
- `pool_pre_ping=True` catches stale connections (common after Docker
  Compose restarts) with one cheap round trip before every checkout.
- `future=True` opts into SQLAlchemy 2's return-row behavior.
- No ORM. SQLAlchemy Core keeps the schema close to the SQL a reviewer
  reads. See ADR 0004 for rationale.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


# Load .env from the project root. Safe to call multiple times. No-op if
# the file is absent, which is fine in CI where variables come from the
# environment directly.
load_dotenv()


def database_url() -> str:
    """
    Return the connection string from DATABASE_URL.

    Raises if unset. There is intentionally no fallback: an unset
    DATABASE_URL in an application context is a configuration error, not
    something to paper over with a dev default.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env or export "
            "DATABASE_URL in your shell."
        )
    return url


_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def get_engine() -> Engine:
    """Build or return the module-level engine."""
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(
            database_url(),
            pool_pre_ping=True,
            future=True,
        )
        _SessionLocal = sessionmaker(
            bind=_engine, autoflush=False, autocommit=False, future=True,
        )
    return _engine


def reset_engine() -> None:
    """Dispose of the current engine. Tests that need a fresh connection
    pool (for example, after simulating a container restart) call this."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


@contextmanager
def get_session() -> Iterator[Session]:
    """
    Context manager yielding a Session. Commits on clean exit, rolls back
    on any exception. Closes the session unconditionally.
    """
    get_engine()  # ensures _SessionLocal is initialized
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
