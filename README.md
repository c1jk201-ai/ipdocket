# IP Docket System

IP Docket System is a US-centered docketing, matter-management, CRM, and billing/accounting workspace for IP practice operations.

Authentication is local username/password based.

## Scope

- US/USPTO-centered docket and matter management
- USPTO document analysis for filing receipts, Office Actions, Notices of Allowance, IDS review, and core U.S. prosecution deadlines
- Matter documents and attachments
- Deadlines, renewals, worklogs, and internal workflow records
- CRM/client records
- Invoice/accounting workflows
- Admin, role, audit, and operational monitoring pages

## Requirements

- Docker and Docker Compose
- PostgreSQL, Redis, and the app containers provided by `docker-compose.yml`
- Python 3.11+ for local non-Docker development

## Configuration

Copy the example environment file and edit secrets:

```bash
cp .env.example .env
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Required production values:

- `SECRET_KEY`
- `POSTGRES_PASSWORD`
- `REDIS_PASSWORD`
- `DATABASE_URL` when using an external database
- `BASE_URL`
- `LOCAL_ADMIN_USERNAME` and `LOCAL_ADMIN_PASSWORD` only if admin bootstrap is enabled

Local password login is enabled by default. Admin bootstrap is disabled unless explicitly enabled:

```env
ALLOW_PASSWORD_LOGIN=true
LOCAL_ADMIN_BOOTSTRAP_ENABLED=true
LOCAL_ADMIN_USERNAME=admin
LOCAL_ADMIN_PASSWORD=change-me-now
```

For local debugging, opt in explicitly:

```env
FLASK_CONFIG=development
FLASK_DEBUG=True
```

You can also create or reset an admin account from the container or local venv:

```bash
python scripts/create_admin_user.py --username admin --email admin@example.com --role admin
```

## Run With Docker

```bash
docker compose up -d --build
```

The app listens on `APP_BIND_IP:APP_PORT`; the default is `127.0.0.1:5000`.

For source-only Python changes in the bind-mounted runtime:

```bash
make runtime-reload
```

For dependency or image-layer changes:

```bash
docker compose up -d --build app scheduler worker
```

## Database

By default, Docker Compose uses the `db` service and the `POSTGRES_*` values in `.env`.
For a fresh local development database, enable model-based schema bootstrap:

```env
DB_WAIT_ON_START=1
DB_SCHEMA_AUTO_CREATE=1
```

For production, start from an initialized database schema and keep runtime DDL disabled:

```env
DB_SCHEMA_AUTO_CREATE=0
```

## Useful Commands

```bash
docker compose ps
docker compose logs -f app
make runtime-status
make runtime-reload
```

Run a focused Python syntax check:

```bash
python -m compileall app scripts
```
