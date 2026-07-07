from __future__ import annotations

_EN_TRANSLATIONS = {
  "app_title": "Invoice Workspace",
  "dashboard": "Dashboard",
  "invoices": "Invoices",
  "clients": "Clients",
  "templates": "Templates",
  "business_profiles": "Business Profiles",
  "new_invoice": "New Invoice",
  "invoice_number": "Invoice #",
  "internal_reference": "Our Ref",
  "client": "Client",
  "issue_date": "Issue Date",
  "due_date": "Due Date",
  "total": "Total",
  "status": "Status",
  "actions": "Actions",
  "view": "View",
  "edit": "Edit",
  "delete": "Delete",
  "search": "Search",
  "filter": "Filter",
  "status_draft": "Draft",
  "status_sent": "Issued",
  "status_paid": "Paid",
  "status_payment_pending": "Payment Pending",
  "status_tax_issued": "Tax Recorded",
  "status_cash_issued": "Tax Recorded",
  "status_processed": "Tax Recorded",
  "status_void": "Void",
  "status_pre_overdue": "Advanced Costs",
  "service_cost": "Service Fee",
  "admin_fee": "Official Fee",
  "subtotal": "Subtotal",
  "tax": "Sales Tax",
  "grand_total": "Grand Total",
  "description": "Description",
  "quantity": "Qty",
  "unit_price": "Unit Price",
  "discount": "Discount",
  "amount": "Amount",
  "notes": "Notes",
  "save": "Save",
  "cancel": "Cancel",
  "print_pdf": "Print / Save PDF",
  "recent_invoices": "Recent Invoices",
  "view_all": "View All",
  "no_invoices": "No invoices found.",
  "keyword_search": "Keyword Search",
  "keyword_placeholder": "Invoice #, client, matter ref, notes, Our Ref, old Our Ref, or Your Ref",
  "all": "All",
  "sort_by": "Sort By",
  "sort_issue_date": "Issue Date",
  "sort_due_date": "Due Date",
  "sort_amount": "Amount",
  "date_from": "Date From",
  "date_to": "Date To",
  "min_amount": "Min Amount",
  "max_amount": "Max Amount",
  "reset": "Reset",
  "bulk_status_change": "Bulk status change",
  "select_status": "Select Status",
  "change_status": "Change Status",
  "bulk_delete": "Delete Selected Invoices",
  "selected_count": " selected",
  "no_results": "No results found.",
  "invoice": "Invoice",
  "client_info": "Client Information",
  "issuer_info": "Issuer Information",
  "business": "Business",
  "vat_taxable": "Taxable",
  "vat_exempt": "Tax Exempt",
  "service_total": "Service Fee Total",
  "admin_total": "Official Fee Total",
  "vat_service_only": "Sales Tax ({{rate}}%, service only)",
  "bank_account": "Bank Account",
  "mark_paid": "Mark as Paid",
  "publish_print": "Issue and Print",
  "tax_issued_warning": "Tax recording has been completed.",
  "cash_issued_warning": "Tax recording has been completed.",
  "processed_warning": "Tax recording has been completed.",
  "tax_issued_restriction": "Editing and deletion are restricted. Change status from the invoice list.",
  "select_template": "Select Template",
  "select_client": "Select Client",
  "new_client": "New Client",
  "client_name": "Client Name",
  "select_business": "Select Business",
  "invoice_details": "Invoice Details",
  "payment_terms": "Payment Terms",
  "days": "days",
  "vat_rate": "Sales Tax Rate",
  "line_items": "Line Items",
  "add_item": "Add Item",
  "item_type": "Type",
  "notes_formatting": "Formatting Supported",
  "formatting_help": "Formatting: **Bold** | *Italic* | Line break: Enter | - List | 1. Numbered list",
  "address": "Address",
  "contact": "Contact",
  "manager": "Manager",
  "email": "Email",
  "service_cost_vat_taxable": "Service Fee",
  "admin_fee_vat_exempt": "Official Fee",
  "foreign_cost_vat_exempt": "Foreign Expenses",
  "billing_total": "Total Amount Due",
  "invoice_total": "Invoice Total",
  "deposit_used": "Retainer Used",
  "amount_due": "Amount Due",
  "service_total_excl_vat": "Service Fee Total (Excl. Tax)",
  "service_total_incl_vat": "Service Fee Total (Tax {{rate}}%)",
}


TRANSLATIONS = {
  "en": _EN_TRANSLATIONS,
}


def get_locale():
  """Return the active locale. U.S. deployments default to English."""
  from flask import request, session

  locale = session.get("lang")
  if locale and locale in TRANSLATIONS:
    return locale

  if request:
    for lang in request.accept_languages:
      code = (lang[0] or "")[:2].lower()
      if code in TRANSLATIONS:
        return code

  return "en"


def _interpolate(text: str, kwargs: dict) -> str:
  for key, value in kwargs.items():
    text = text.replace("{{" + key + "}}", str(value))
  return text


def t(key, **kwargs):
  text = TRANSLATIONS.get(get_locale(), _EN_TRANSLATIONS).get(key, key)
  return _interpolate(text, kwargs)


def t_lang(key, lang, **kwargs):
  text = TRANSLATIONS.get((lang or "en").lower(), _EN_TRANSLATIONS).get(key, key)
  return _interpolate(text, kwargs)
