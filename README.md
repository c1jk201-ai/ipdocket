# IP Docket System

IP Docket System is a US-centered IP operations workspace for docketing,
matter management, USPTO document review, CRM, workflow tracking, renewals,
and billing/accounting. Authentication is local username/password based.

## Main Surfaces

- Matters and dockets under `/case`, including configurable matter creation,
  matter detail pages, documents, communications, deadlines, and status history.
- USPTO-focused document analysis for filing receipts, Office Actions, Notices
  of Allowance, IDS review, and core U.S. prosecution deadlines.
- Workflow, worklog, renewal, deadline, statistics, and management dashboards.
- CRM/client records and invoice/accounting workflows under `/crm`,
  `/business`, `/accounting`, and `/accounting/invoice-system`.
- Admin pages for users, roles, audit logs, operational health, configuration,
  matter menu parameters, and security checks.

## Runtime Stack

The normal local runtime is Docker Compose:

- `app`: Flask/Gunicorn web application on `APP_BIND_IP:APP_PORT`
- `scheduler`: background scheduler process
- `worker`: deferred and annuity queue worker
- `db`: PostgreSQL
- `redis`: shared rate-limit and queue support
- `clamav`: upload scanning support

The default app URL is `http://127.0.0.1:5000`.

## Requirements

- Docker Desktop with Docker Compose
- Python 3.11+ for local test and non-Docker development
- Windows users should run commands from PowerShell in this repository root

## Quick Start

Copy the environment template:

```powershell
Copy-Item .env.example .env
```

Edit `.env` before starting the stack. At minimum, replace these values:

- `SECRET_KEY`
- `POSTGRES_PASSWORD`
- `REDIS_PASSWORD`
- `BASE_URL`

For a fresh local database, keep schema bootstrap enabled:

```env
DB_WAIT_ON_START=1
DB_SCHEMA_AUTO_CREATE=1
```

Start the full stack:

```powershell
docker compose up -d --build
```

Check that the containers are running:

```powershell
docker compose ps
```

Health endpoints:

- `GET /health`: cheap liveness check
- `GET /ready`: readiness status, returns `503` when dependencies are not ready
- `GET /internal/ready`: detailed readiness checks for operators

Create or reset an admin account from the container or local virtualenv:

```powershell
python scripts/create_admin_user.py --username admin --email admin@example.com --role admin
```

Admin bootstrap is disabled by default. Enable it only for controlled local
setup or one-time bootstrap:

```env
ALLOW_PASSWORD_LOGIN=true
LOCAL_ADMIN_BOOTSTRAP_ENABLED=true
LOCAL_ADMIN_USERNAME=admin
LOCAL_ADMIN_PASSWORD=change-me-now
```

## Docker Notes

Compose loads `.env`, but the containers use the internal `db` service for
`DATABASE_URL`. The `POSTGRES_*` values in `.env` control the bundled database.
If you use an external database, adjust the deployment configuration rather
than relying only on the local `.env` `DATABASE_URL`.

The local override file bind-mounts source directories into `app`, `scheduler`,
and `worker`, so many Python/template changes only need a runtime restart:

```powershell
make runtime-reload
```

For dependency, Dockerfile, entrypoint, or image-layer changes, rebuild the
affected services:

```powershell
docker compose up -d --build app scheduler worker
```

If a live route still shows stale behavior after a source edit, restart the app
process first and then check logs:

```powershell
docker compose restart app
docker compose logs -f app
```

## Local Development

Use the repo virtualenv when present:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
```

Run focused checks:

```powershell
.\.venv\Scripts\python.exe -m compileall app scripts
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

The Makefile uses `.venv\Scripts\python.exe` automatically on Windows when it
exists:

```powershell
make test
make lint
make quality
```

For direct Flask execution outside Docker, ensure PostgreSQL and Redis are
available and the environment points at the intended services:

```powershell
$env:FLASK_CONFIG = "development"
$env:FLASK_DEBUG = "True"
.\.venv\Scripts\python.exe run.py
```

## Configuration

Important local and production settings live in `.env.example` and
`.env.production.example`.

- `TIMEZONE`, `LOCALE`, and date formats default to U.S. operating conventions.
- `ALLOW_PASSWORD_LOGIN=true` enables local username/password login.
- `LOCAL_ADMIN_BOOTSTRAP_ENABLED=false` prevents accidental admin creation.
- `DB_SCHEMA_AUTO_CREATE=1` is suitable for fresh local development only.
- `DB_SCHEMA_AUTO_CREATE=0` should be used for production and initialized
  schemas.
- `RATELIMIT_REQUIRE_SHARED_STORAGE=1` expects Redis-backed rate limiting.
- `INVOICEAPP_INTEGRATED=1` enables the integrated invoice/accounting module.

Production deployments should start from `.env.production.example`, replace all
secrets, set `BASE_URL` to the public origin, keep `FLASK_DEBUG=False`, and keep
runtime DDL disabled.

## Repository Layout

- `app/`: Flask application, blueprints, services, models, static assets, and
  templates.
- `app_config/`: runtime configuration files managed by the app.
- `scripts/`: admin, quality, database, and operational helper scripts.
- `legacy_billing_schema/`: compatibility and migration helpers for the invoice
  subsystem.
- `tests/`: unit, integration, architecture, and route coverage.
- `docs/`: operational notes, compatibility boundaries, and third-party notices.
- `data/`, `logs/`, `uploads/`, `instance/`: local runtime state. Treat these as
  environment data, not source.

## Operations Checklist

```powershell
docker compose ps
docker compose logs -f app
docker compose logs -f scheduler
docker compose logs -f worker
make runtime-status
make runtime-reload
```

Useful authenticated admin pages:

- `/admin/`
- `/admin/ops/`
- `/admin/ops/service-health`
- `/admin/security/health`

Use `docs/LEGACY_COMPATIBILITY.md` before changing compatibility routes or
legacy accounting/case adapters.
