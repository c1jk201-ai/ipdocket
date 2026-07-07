from __future__ import annotations

from urllib.parse import quote_plus

from flask import current_app
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.services.billing.invoice_prefill import (
    build_invoice_create_url,
    resolve_invoice_create_base_url,
)
from app.utils.error_logging import report_swallowed_exception


def _rollback_session() -> None:
    try:
        db.session.rollback()
    except SQLAlchemyError as exc:
        report_swallowed_exception(
            exc,
            context="case.detail_finance.rollback_session",
            log_key="case.detail_finance.rollback_session",
            log_window_seconds=300,
        )


def _finance_display_sort_key(
    item: dict, *, date_keys: tuple[str, ...], id_keys: tuple[str, ...] = ()
) -> tuple[str, int]:
    date_token = "9999-12-31"
    for key in date_keys:
        raw = str((item or {}).get(key) or "").strip()
        if raw:
            date_token = raw[:10]
            break

    numeric_id = 0
    for key in id_keys:
        raw = (item or {}).get(key)
        try:
            numeric_id = int(raw or 0)
            break
        except Exception:
            continue

    return (date_token, numeric_id)


def _sort_finance_rows_for_display(
    rows: list[dict], *, date_keys: tuple[str, ...], id_keys: tuple[str, ...] = ()
) -> list[dict]:
    ordered = list(rows or [])
    ordered.sort(
        key=lambda item: _finance_display_sort_key(item, date_keys=date_keys, id_keys=id_keys)
    )
    return ordered


def build_costs_section(ctx: dict, *, summary_only: bool = False) -> dict:
    mid_str = ctx["_mid_str"]
    matter = ctx["matter"]

    invoice_module_list_url = None
    invoice_module_new_url = None
    try:
        base = (
            current_app.config.get("INVOICE_MODULE_VIEW_BASE_URL")
            or "/accounting/invoice-system/invoices"
        ).strip()
        base = base.rstrip("/")
        if base:
            invoice_module_list_url = (
                f"{base}Newipm_case_id={quote_plus(mid_str)}" if mid_str else base
            )
            invoice_module_new_url = build_invoice_create_url(
                resolve_invoice_create_base_url(config=current_app.config),
                matter=matter,
                matter_id=mid_str,
            )
    except Exception:
        invoice_module_list_url = None
        invoice_module_new_url = None

    case_finance = {}
    try:
        from app.services.billing.case_finance_service import CaseFinanceService

        case_finance = CaseFinanceService.get_summary(mid_str, include_ledger=not summary_only)
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Error in CaseFinanceService.get_summary: {e}")
        case_finance = {
            "summary": {},
            "invoices": [],
            "payables": [],
            "ledger": [],
        }

    if summary_only:
        case_finance_invoices = []
        case_finance_payables = []
        case_finance_ledger = []
    else:
        case_finance_invoices = _sort_finance_rows_for_display(
            case_finance.get("invoices") or [],
            date_keys=("issue_date", "due_date"),
            id_keys=("invoice_id",),
        )
        case_finance_payables = _sort_finance_rows_for_display(
            case_finance.get("payables") or [],
            date_keys=("expense_date", "dn_date", "due_date"),
            id_keys=("expense_id",),
        )
        case_finance_ledger = _sort_finance_rows_for_display(
            case_finance.get("ledger") or [],
            date_keys=("date",),
            id_keys=("invoice_id", "expense_id"),
        )

    return {
        "case_finance": case_finance,
        "case_finance_summary": case_finance.get("summary") or {},
        "case_finance_invoices": case_finance_invoices,
        "case_finance_payables": case_finance_payables,
        "case_finance_ledger": case_finance_ledger,
        "invoice_module_list_url": invoice_module_list_url,
        "invoice_module_new_url": invoice_module_new_url,
    }
