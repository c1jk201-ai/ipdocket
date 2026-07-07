"""
Invoice Models - Facade Layer

These models serve as a compatibility layer for legacy code that references
the internal SQLAlchemy invoice models. The actual data is now stored in
the billing_invoices SQLite tables.

For new code, use the service layer instead:
    from app.services.billing.invoice_services import InvoiceService, PaymentService

DEPRECATION NOTICE:
    Direct use of these models for database operations is deprecated.
    They are maintained for backward compatibility only.
"""

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from app.extensions import db

# Import service layer for facade operations
try:
    from app.services.billing.invoice_services import (
        IntegrationService,
        InvoiceLinkService,
        InvoiceService,
        PaymentService,
    )

    _SERVICES_AVAILABLE = True
    _SERVICES_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:
    _SERVICES_AVAILABLE = False
    _SERVICES_IMPORT_ERROR = exc


class InvoiceFacadeUnavailableError(RuntimeError):
    """Raised when the canonical billing service layer cannot be imported."""


def _require_billing_services() -> None:
    if _SERVICES_AVAILABLE:
        return
    raise InvoiceFacadeUnavailableError(
        "Invoice facade requires app.services.billing.invoice_services; "
        "canonical invoice data is not available through the legacy SQLAlchemy model."
    ) from _SERVICES_IMPORT_ERROR


class Invoice(db.Model):
    """
    Invoice model - Facade over billing_invoices.invoices table.

    This model is kept for backward compatibility. For new code, use
    InvoiceService and matter_id-based links from app.services.billing.
    """

    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"))
    case_id = db.Column(db.Integer, db.ForeignKey("cases.id"))
    issue_date = db.Column(db.Date, default=date.today)
    due_date = db.Column(db.Date)
    status = db.Column(db.String(20), default="draft")
    currency = db.Column(db.String(8), default="USD")
    total = db.Column(db.Numeric(15, 2), default=0)
    tax_no = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)
    deleted_op_id = db.Column(db.Integer, index=True)

    items = db.relationship("InvoiceItem", backref="invoice", cascade="all, delete-orphan")
    payments = db.relationship("Payment", backref="invoice", cascade="all, delete-orphan")

    # ----- Facade Methods -----

    @classmethod
    def get_from_billing(cls, invoice_id: int) -> Optional[Dict[str, Any]]:
        """
        Fetch invoice data from the canonical billing_invoices database.
        Returns a dictionary with all invoice fields.
        """
        _require_billing_services()
        return InvoiceService.get_by_id(invoice_id)

    @classmethod
    def list_from_billing(cls, **filters) -> List[Dict[str, Any]]:
        """
        List invoices from the canonical billing_invoices database.

        Supported filters:
            client_id, status, billing_status, payment_status,
            date_from, date_to, limit, offset
        """
        _require_billing_services()
        return InvoiceService.list_invoices(**filters)

    def get_billing_data(self) -> Optional[Dict[str, Any]]:
        """Get the corresponding billing_invoices record for this invoice."""
        _require_billing_services()
        if not self.id:
            return None
        return InvoiceService.get_by_id(self.id)

    def get_case_links(self) -> List[Any]:
        """Get N:N case links from invoice_case_map."""
        _require_billing_services()
        if not self.id:
            return []
        return InvoiceLinkService.get_links(self.id)

    def get_payment_records(self) -> List[Any]:
        """Get normalized payment records from invoice_payments."""
        _require_billing_services()
        if not self.id:
            return []
        return PaymentService.get_payments(self.id)

    def calculate_outstanding(self) -> Dict[str, Any]:
        """Calculate paid total and outstanding amount."""
        _require_billing_services()
        if not self.id:
            return {"total_minor": 0, "paid_total": 0, "outstanding": 0}
        return InvoiceService.calculate_totals(self.id)


class InvoiceItem(db.Model):
    """
    Invoice line item model - Facade over billing_invoices.line_items.

    For new code, use InvoiceService.get_line_items().
    """

    __tablename__ = "invoice_items"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    description = db.Column(db.String(200))
    qty = db.Column(db.Numeric(10, 2), default=1)
    unit_price = db.Column(db.Numeric(15, 2), default=0)
    amount = db.Column(db.Numeric(15, 2), default=0)
    case_id = db.Column(db.Integer, db.ForeignKey("cases.id"))


class Payment(db.Model):
    """
    Payment model - Facade over billing_invoices.invoice_payments.

    For new code, use PaymentService from app.services.billing.invoice_services.
    """

    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    paid_date = db.Column(db.Date, default=date.today)
    amount = db.Column(db.Numeric(15, 2), default=0)
    method = db.Column(db.String(50))
    note = db.Column(db.String(200))
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)
    deleted_op_id = db.Column(db.Integer, index=True)

    @classmethod
    def get_for_invoice_from_billing(cls, invoice_id: int) -> List[Any]:
        """Get payments from canonical invoice_payments table."""
        _require_billing_services()
        return PaymentService.get_payments(invoice_id)


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------


def get_unified_invoice(invoice_id: int) -> Optional[Dict[str, Any]]:
    """
    Get comprehensive invoice data from the unified billing system.

    Returns a dictionary containing:
        - invoice: Core invoice data
        - line_items: List of line items
        - payments: List of payment records
        - case_links: List of case/matter links
        - integrations: List of external integrations
        - totals: Calculated totals (paid, outstanding)
    """
    _require_billing_services()

    invoice = InvoiceService.get_by_id(invoice_id)
    if not invoice:
        return None

    return {
        "invoice": invoice,
        "line_items": InvoiceService.get_line_items(invoice_id),
        "payments": [
            p.__dict__ if hasattr(p, "__dict__") else p
            for p in PaymentService.get_payments(invoice_id)
        ],
        "case_links": [
            l.__dict__ if hasattr(l, "__dict__") else l
            for l in InvoiceLinkService.get_links(invoice_id)
        ],
        "integrations": [
            i.__dict__ if hasattr(i, "__dict__") else i
            for i in IntegrationService.get_integrations(invoice_id)
        ],
        "totals": InvoiceService.calculate_totals(invoice_id),
    }
