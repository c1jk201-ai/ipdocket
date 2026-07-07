#!/bin/sh

set -eu

DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${POSTGRES_USER:-ipm}"
DB_NAME="${POSTGRES_DB:-ipm}"
BACKUP_PATH="${BACKUP_PATH:-/backup/backup.sql}"
RESTORE_ENABLED="${DB_RESTORE_ON_START:-1}"
RESTORE_FORCE="${DB_RESTORE_FORCE:-0}"

if [ "$RESTORE_ENABLED" = "0" ]; then
  echo "[db-restore] DB_RESTORE_ON_START=0 -> skip restore"
  exit 0
fi

if [ ! -s "$BACKUP_PATH" ]; then
  echo "[db-restore] backup.sql not found or empty -> skip restore"
  exit 0
fi

export PGPASSWORD="${POSTGRES_PASSWORD:-}"

echo "[db-restore] Waiting for DB (${DB_HOST}:${DB_PORT})..."
i=0
until pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -ge 60 ]; then
    echo "[db-restore] DB not ready after 120s -> abort"
    exit 1
  fi
  sleep 2
done

if [ "$RESTORE_FORCE" = "1" ]; then
  echo "[db-restore] DB_RESTORE_FORCE=1 -> forcing restore"
else
  # Restore should happen only for a brand-new empty DB volume.
  # Using invoice-table presence/count is fragile (the invoice subsystem may live elsewhere),
  # and can cause a full DB restore on every deploy, wiping admin changes (e.g. user roles).
  #
  # Empty heuristic: if the DB already has any non-system tables, treat it as initialized.
  non_system_tables="$(
    psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tA 2>/dev/null \
      -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema') AND table_type='BASE TABLE';" \
    | tr -d ' '
  )"

  case "$non_system_tables" in
    ''|*[!0-9]*)
      echo "[db-restore] Could not determine table count (got: '${non_system_tables:-<empty>}') -> abort"
      exit 1
      ;;
  esac

  if [ "$non_system_tables" -gt 0 ]; then
    echo "[db-restore] DB already initialized (tables=$non_system_tables) -> skip restore"
    exit 0
  fi
fi

echo "[db-restore] Restoring DB from $BACKUP_PATH..."
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -f "$BACKUP_PATH"
echo "[db-restore] Restore complete"
