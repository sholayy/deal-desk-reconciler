#!/bin/bash
# Runs on first PostgreSQL volume initialization. Never wipe a Docker volume
# merely to add these databases to an existing installation.
set -euo pipefail

for database in deal_desk_test deal_desk_migration_test; do
  if ! psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
      -tAc "SELECT 1 FROM pg_database WHERE datname = '$database'" | grep -q '^1$'; then
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
      -c "CREATE DATABASE $database"
  fi
done
