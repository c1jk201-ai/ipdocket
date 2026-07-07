from __future__ import annotations

from typing import Any

FINAL_TAX_BILLING_STATUSES = {"tax_issued", "cash_issued", "processed"}

TAX_ISSUE_TYPE_LABELS_EN = {
  "tax_invoice": "Tax documentation",
  "cash_receipt": "Payment receipt",
  "card": "Card payment",
  "non_taxable": "Non-taxable",
  "legacy_processed": "Legacy processed",
}

TAX_ISSUE_TYPE_OPTIONS = tuple(TAX_ISSUE_TYPE_LABELS_EN.keys())


def _norm(value: Any) -> str:
  try:
    return str(value or "").strip().lower()
  except Exception:
    return ""


def normalize_tax_issue_type(value: Any, billing_status: Any = None) -> str:
  """Return canonical tax issue type, with legacy billing-status fallback."""
  issue_type = _norm(value)
  if issue_type in TAX_ISSUE_TYPE_LABELS_EN:
    return issue_type

  billing = _norm(billing_status)
  if billing == "tax_issued":
    return "tax_invoice"
  if billing == "cash_issued":
    return "cash_receipt"
  if billing == "processed":
    return "legacy_processed"
  return ""


def tax_issue_type_label(
  value: Any,
  billing_status: Any = None,
  *,
  locale: str = "en",
) -> str:
  issue_type = normalize_tax_issue_type(value, billing_status)
  if not issue_type:
    return ""
  return TAX_ISSUE_TYPE_LABELS_EN.get(issue_type, issue_type)


def tax_issue_source_label(value: Any, *, locale: str = "en") -> str:
  source = _norm(value)
  if not source:
    source = "legacy"
  return source.replace("_", " ")


def effective_tax_billing_status(billing_status: Any) -> str:
  """Display legacy cash/processed completion rows as tax-recorded completion."""
  billing = _norm(billing_status)
  if billing in FINAL_TAX_BILLING_STATUSES:
    return "tax_issued"
  return billing


def enrich_invoice_tax_issue_fields(row: dict[str, Any] | None) -> dict[str, Any] | None:
  if row is None:
    return row
  billing = _norm(row.get("billing_status") or row.get("status"))
  display_billing = effective_tax_billing_status(billing)
  issue_type = normalize_tax_issue_type(row.get("tax_issue_type"), billing)
  row["billing_status_display"] = display_billing or billing
  row["is_tax_issue_done"] = billing in FINAL_TAX_BILLING_STATUSES
  row["tax_issue_type_resolved"] = issue_type
  row["tax_issue_type_label"] = tax_issue_type_label(issue_type, billing)
  source = row.get("tax_issue_source")
  row["tax_issue_source_label"] = tax_issue_source_label(source if source else "legacy")
  return row
