"""
Alembic environment configuration.

Resolves the database URL from DATABASE_URL at runtime so migrations run
against whichever database the shell has configured (local Docker Compose
by default). Targets the metadata in `db/models.py` so autogenerate can
diff against it, though we review every migration by hand per the
reviewer's Phase 2 guidance.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from alembic import context

# Load .env from the project root so Alembic reads the same source of
# truth as db/session.py. No-op if .env is absent (CI reads env directly).
load_dotenv()

# db/ is on sys.path because alembic.ini sets prepend_sys_path=.
from db.models import metadata as target_metadata  # noqa: E402

config = context.config

# Resolve DATABASE_URL from the environment. Fail-closed: there is no
# hardcoded fallback. An unset DATABASE_URL is a configuration error, not
# something to paper over with a dev default. Pass ALEMBIC_DATABASE_URL to
# target a different database (e.g., the test database) for a specific
# invocation.
database_url = os.environ.get("ALEMBIC_DATABASE_URL") or os.environ.get(
    "DATABASE_URL"
)
if not database_url:
    raise RuntimeError(
        "DATABASE_URL (or ALEMBIC_DATABASE_URL) is not set. Copy "
        ".env.example to .env or export the variable in your shell."
    )
config.set_main_option("sqlalchemy.url", database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
