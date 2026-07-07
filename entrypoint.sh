#!/bin/sh


set -eu

is_true() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

if [ "$#" -eq 0 ]; then
  WORKERS="${GUNICORN_WORKERS:-4}"
  THREADS="${GUNICORN_THREADS:-}"
  WORKER_CLASS="${GUNICORN_WORKER_CLASS:-}"
  TIMEOUT="${GUNICORN_TIMEOUT:-60}"
  GRACEFUL_TIMEOUT="${GUNICORN_GRACEFUL_TIMEOUT:-30}"
  KEEPALIVE="${GUNICORN_KEEPALIVE:-2}"
  MAX_REQUESTS="${GUNICORN_MAX_REQUESTS:-0}"
  MAX_REQUESTS_JITTER="${GUNICORN_MAX_REQUESTS_JITTER:-0}"
  PORT="${PORT:-5000}"

  # If threads are configured, default to gthread worker (unless explicitly overridden).
  if [ -n "${THREADS}" ] && [ -z "${WORKER_CLASS}" ]; then
    WORKER_CLASS="gthread"
  fi

  set -- gunicorn -w "$WORKERS" -b "0.0.0.0:${PORT}"
  if [ -n "${WORKER_CLASS}" ]; then
    set -- "$@" -k "$WORKER_CLASS"
  fi
  if [ -n "${THREADS}" ]; then
    set -- "$@" --threads "$THREADS"
  fi
  if [ -n "${TIMEOUT}" ]; then
    set -- "$@" --timeout "$TIMEOUT"
  fi
  if [ -n "${GRACEFUL_TIMEOUT}" ]; then
    set -- "$@" --graceful-timeout "$GRACEFUL_TIMEOUT"
  fi
  if [ -n "${KEEPALIVE}" ]; then
    set -- "$@" --keep-alive "$KEEPALIVE"
  fi
  if [ -n "${MAX_REQUESTS}" ]; then
    set -- "$@" --max-requests "$MAX_REQUESTS"
  fi
  if [ -n "${MAX_REQUESTS_JITTER}" ]; then
    set -- "$@" --max-requests-jitter "$MAX_REQUESTS_JITTER"
  fi
  set -- "$@" run:app
fi

# Optional DB wait guardrail.
# - DB_WAIT_ON_START: wait for Postgres before starting (default: true)
# - DB_WAIT_TIMEOUT_SECONDS: max wait (default: 120)

if is_true "${DB_WAIT_ON_START:-1}"; then
  echo "Waiting for database connectivity..."
  python scripts/wait_for_db.py
fi

echo "Starting: $*"
exec "$@"
