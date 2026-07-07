# Legacy Compatibility Boundaries

This document tracks compatibility code that intentionally supports pre-canonical
legacy callers.

## Rules

- New code should use canonical `Matter.matter_id` routes and service-layer APIs.
- Legacy compatibility endpoints should live in modules named `legacy_*`.
- Legacy compatibility HTTP endpoints should use `legacy_compat_endpoint()` so
  responses include:
  - `Deprecation: true`
  - `X-IPM-Legacy-Compat: <compat-id>`
  - `Link: <successor>; rel="successor-version"` when a successor exists
- Do not import the legacy `Case` model or `app.models.invoice` facade from
  general-purpose route modules. Keep those imports inside explicit compatibility
  modules or service adapters.

## Active HTTP Compatibility Endpoints

| Endpoint | Module | Compat ID | Successor |
| --- | --- | --- | --- |
| `POST /accounting/api/invoices` | `app/blueprints/accounting/legacy_api.py` | `accounting-invoice-api` | `/accounting/invoice-system/invoices` |
| `PATCH/DELETE /accounting/api/invoices/<iid>` | `app/blueprints/accounting/legacy_api.py` | `accounting-invoice-api` | `/accounting/invoice-system/invoices` |
| `GET /api/case/<case_id>/relations` | `app/blueprints/api/legacy_case.py` | `api-case-id` | `/api/cases/<matter_id>/summary` |

## Guardrails

- `tests/unit/test_matter_case_boundary.py` ratchets legacy `Case` and invoice
  facade imports.
- `tests/unit/test_accounting_legacy_api_validation.py` verifies the accounting
  compatibility API stays marked as deprecated.
- `tests/unit/test_legacy_compat_headers.py` verifies legacy Case-id API headers.
