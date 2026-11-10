"""Database safety checks that do not need a running Postgres instance."""

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from db.models import observations
from conftest import _test_database_url


def test_test_db_url_rejects_development_database(monkeypatch):
    url = "postgresql+psycopg2://user:pass@localhost:5432/deal_desk"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("TEST_DATABASE_URL", url)
    assert _test_database_url() is None


def test_test_db_url_rejects_migration_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://user:pass@localhost:5432/deal_desk")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg2://user:pass@localhost:5432/deal_desk_migration_test")
    assert _test_database_url() is None


def test_test_db_url_accepts_dedicated_test_database(monkeypatch):
    url = "postgresql+psycopg2://user:pass@localhost:5432/deal_desk_test"
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://user:pass@localhost:5432/deal_desk")
    monkeypatch.setenv("TEST_DATABASE_URL", url)
    monkeypatch.setenv("MIGRATION_TEST_DATABASE_URL", "postgresql+psycopg2://user:pass@localhost:5432/deal_desk_migration_test")
    assert _test_database_url() == url


def test_fk_delete_clears_only_run_link():
    ddl = str(CreateTable(observations).compile(dialect=postgresql.dialect()))
    assert "ON DELETE SET NULL (reconciliation_run_id)" in ddl
    assert "tenant_id VARCHAR(64) NOT NULL" in ddl
