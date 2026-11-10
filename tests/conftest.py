"""
Shared pytest fixtures.

The `db_session` fixture enables the Phase 2 persistence tests. It
enforces the reviewer's correction-patch rule that tests must never
write to the development database:

- `TEST_DATABASE_URL` must be set.
- Its database name must end with `_test` (fail-closed naming guard).
- Connection must succeed. If any of these fails, the DB tests skip
  cleanly rather than target the dev database.

Fast unit tests (classifier, handoff, API, config) do not use this
fixture and run in all environments.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from sqlalchemy.engine import make_url

import pytest
from dotenv import load_dotenv


load_dotenv()


def _test_database_url() -> str | None:
    """
    Return TEST_DATABASE_URL if it is set and names a database whose name
    ends with '_test'. The suffix check is a fail-closed guard against
    the environment variable being accidentally set to the dev URL.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        return None
    try:
        db_name = make_url(url).database or ""
        dev_url = os.environ.get("DATABASE_URL")
        migration_url = os.environ.get("MIGRATION_TEST_DATABASE_URL")
        if (not db_name.endswith("_test") or db_name.endswith("_migration_test")
                or (dev_url and make_url(dev_url) == make_url(url))
                or (migration_url and make_url(migration_url) == make_url(url))):
            return None
    except Exception:
        return None
    return url


def _postgres_reachable() -> bool:
    """Try a lightweight connection against the test database."""
    url = _test_database_url()
    if not url:
        return False

    # Build a throwaway engine so the main engine stays bound to whatever
    # DATABASE_URL is (the app's dev DB), and tests never flip it.
    from sqlalchemy import create_engine, text
    try:
        engine = create_engine(url, pool_pre_ping=True, future=True)
        with engine.connect() as conn:
            actual = conn.execute(text("SELECT current_database()" )).scalar_one()
            if actual != make_url(url).database:
                raise RuntimeError("TEST_DATABASE_URL does not identify the connected database")
        engine.dispose()
        return True
    except Exception:
        return False


_TEST_DB_AVAILABLE: bool | None = None


def test_db_available() -> bool:
    """Cache the availability check per test session."""
    global _TEST_DB_AVAILABLE
    if _TEST_DB_AVAILABLE is None:
        _TEST_DB_AVAILABLE = _postgres_reachable()
    return _TEST_DB_AVAILABLE


def _skip_reason() -> str:
    if not os.environ.get("TEST_DATABASE_URL"):
        return (
            "TEST_DATABASE_URL is not set. Copy .env.example to .env and "
            "set TEST_DATABASE_URL to a dedicated test database whose name "
            "ends with '_test'."
        )
    url = os.environ.get("TEST_DATABASE_URL", "")
    parsed = urlparse(url)
    if not parsed.path.lstrip("/").endswith("_test"):
        return (
            "TEST_DATABASE_URL does not point at a database whose name "
            "ends with '_test'. Refusing to run writes against a possibly "
            "non-test database."
        )
    return (
        "Postgres is not reachable at TEST_DATABASE_URL. Start the stack "
        "with `docker compose up -d` and run `alembic upgrade head` "
        "against the test database."
    )


@pytest.fixture(scope="session")
def test_engine():
    """
    Build an engine for the test database once per session. Fixture scope
    is intentionally 'session' so Alembic (run in the setup step) and
    every test share the same database.
    """
    if not test_db_available():
        pytest.skip(_skip_reason())

    from sqlalchemy import create_engine
    engine = create_engine(_test_database_url(), pool_pre_ping=True, future=True)
    # Fail fast if only the database was created but test migrations were omitted.
    from sqlalchemy import inspect
    required = {"reconciliation_runs", "source_events", "observations"}
    missing = required - set(inspect(engine).get_table_names())
    if missing:
        engine.dispose()
        pytest.fail("Test DB schema missing: " + ", ".join(sorted(missing)) +
                    ". Run `ALEMBIC_DATABASE_URL=$TEST_DATABASE_URL alembic upgrade head` before pytest.")
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(test_engine):
    """
    Yields a Session on a connection that will be rolled back on teardown.
    The whole test runs inside a transaction that never commits, so no
    test pollutes the next one.

    Uses the test engine specifically (bound to TEST_DATABASE_URL),
    never the app's dev engine.
    """
    from sqlalchemy.orm import Session as SASession

    connection = test_engine.connect()
    transaction = connection.begin()
    session = SASession(bind=connection)

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture(scope="session")
def migration_test_url(test_engine):
    """Separate disposable database for destructive Alembic round-trip tests.

    Depends on test_engine so ordinary integration tests still skip without
    PostgreSQL, but a missing/incorrect migration DB is a hard failure once
    the integration environment is available.
    """
    from sqlalchemy import create_engine, text
    url = os.environ.get("MIGRATION_TEST_DATABASE_URL")
    if not url:
        pytest.fail("MIGRATION_TEST_DATABASE_URL is required for migration tests")
    try:
        parsed = make_url(url)
        if parsed.database != "deal_desk_migration_test":
            pytest.fail("Migration tests require exactly deal_desk_migration_test")
        test_url = make_url(_test_database_url())
        dev_url = make_url(os.environ["DATABASE_URL"])
        if parsed == test_url or parsed == dev_url:
            pytest.fail("Migration-test database must differ from app and integration-test DB")
        # Require same server/user as TEST_DATABASE_URL, but a distinct DB.
        if (parsed.host, parsed.port, parsed.username) != (test_url.host, test_url.port, test_url.username):
            pytest.fail("Migration-test DB must use the same local Postgres instance as TEST_DATABASE_URL")
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            actual = conn.execute(text("SELECT current_database()" )).scalar_one()
            if actual != "deal_desk_migration_test":
                pytest.fail("Connected to unexpected database for destructive migration tests")
        engine.dispose()
    except Exception as exc:
        pytest.fail(f"Migration test DB is not reachable or misconfigured: {exc}")
    return url
