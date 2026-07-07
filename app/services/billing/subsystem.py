from __future__ import annotations

from dataclasses import asdict, dataclass

from flask import Flask

from app.services.billing.db_core import _get_column_names as _invoice_get_column_names
from app.services.billing.db_core import _table_exists as _invoice_table_exists
from app.services.billing.db_core import get_db as _invoice_get_db


@dataclass(frozen=True)
class BillingSubsystemState:
  enabled: bool
  ready: bool
  has_profiles: bool = False
  has_invoices: bool = False
  needs_schema_migration: bool = False
  testing: bool = False
  skipped_reason: str | None = None

  def as_dict(self) -> dict[str, object]:
    return asdict(self)


def _is_testing(app: Flask) -> bool:
  return bool(app.config.get("TESTING"))


def _store_state(app: Flask, state: BillingSubsystemState) -> BillingSubsystemState:
  app.extensions["billing_subsystem"] = state.as_dict()
  app.config["INVOICEAPP_INIT_OK"] = bool(state.ready)
  return state


def get_billing_subsystem_state(app: Flask) -> BillingSubsystemState:
  raw = app.extensions.get("billing_subsystem")
  if isinstance(raw, dict):
    try:
      return BillingSubsystemState(**raw)
    except TypeError:
      pass
  enabled = bool(app.config.get("INVOICEAPP_INTEGRATED"))
  ready = bool(app.config.get("INVOICEAPP_INIT_OK"))
  return BillingSubsystemState(enabled=enabled, ready=(not enabled) or ready)


def billing_subsystem_enabled(app: Flask) -> bool:
  return bool(get_billing_subsystem_state(app).enabled)


def billing_subsystem_ready(app: Flask) -> bool:
  state = get_billing_subsystem_state(app)
  return (not state.enabled) or bool(state.ready)


def initialize_billing_subsystem(app: Flask) -> BillingSubsystemState:
  if _is_testing(app):
    app.logger.info("Invoice integration connectivity check skipped in testing.")
    return _store_state(
      app,
      BillingSubsystemState(
        enabled=bool(app.config.get("INVOICEAPP_INTEGRATED")),
        ready=True,
        testing=True,
        skipped_reason="testing",
      ),
    )

  if not app.config.get("INVOICEAPP_INTEGRATED"):
    return _store_state(
      app,
      BillingSubsystemState(enabled=False, ready=False, skipped_reason="disabled"),
    )

  try:
    with app.app_context():
      conn = _invoice_get_db()
      try:
        has_profiles = _invoice_table_exists(conn, "business_profile")
        has_invoices = _invoice_table_exists(conn, "invoices")
        needs_schema_migration = False
        if has_profiles and has_invoices:
          journal_cols = _invoice_get_column_names(conn, "journal_entries")
          required_journal_cols = {
            "approved",
            "posted",
            "reversed",
            "locked_period",
          }
          needs_schema_migration = not _invoice_table_exists(
            conn, "accounting_periods"
          ) or not required_journal_cols.issubset(set(journal_cols))
      finally:
        try:
          conn.close()
        except Exception as exc:
          app.logger.debug(
            "Swallowed exception in initialize_billing_subsystem.close: %s",
            exc,
            exc_info=True,
          )

      if has_profiles and has_invoices and needs_schema_migration:
        app.logger.error(
          "Invoice ERP accounting schema is outdated. "
          "Run the billing migration explicitly before enabling accounting features."
        )

    if not (has_profiles and has_invoices):
      app.logger.error(
        "Invoice integration enabled but base tables are missing. "
        "Initialize the billing schema before enabling accounting features."
      )

    return _store_state(
      app,
      BillingSubsystemState(
        enabled=True,
        ready=bool(has_profiles and has_invoices and not needs_schema_migration),
        has_profiles=bool(has_profiles),
        has_invoices=bool(has_invoices),
        needs_schema_migration=bool(needs_schema_migration),
      ),
    )
  except Exception:
    app.logger.exception("Invoice module connectivity check failed; integration disabled.")
    return _store_state(
      app,
      BillingSubsystemState(enabled=True, ready=False, skipped_reason="connectivity_error"),
    )
