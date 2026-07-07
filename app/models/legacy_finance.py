import uuid
from datetime import datetime

from app.extensions import db
from app.utils.policy_sql import policy_text as text


class LegacyInvoice(db.Model):
    __tablename__ = "invoice"

    invoice_id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    matter_id = db.Column(db.Text, nullable=False, index=True)
    external_invoice_id = db.Column(db.Integer, index=True)
    external_invoice_number = db.Column(db.Text)
    external_invoice_url = db.Column(db.Text)

    integrated_fee_ref = db.Column(db.Text)
    fee_ref = db.Column(db.Text)
    bill_date = db.Column(db.Text)
    due_date = db.Column(db.Text)
    tax_issued_date = db.Column(db.Text)
    tax_no = db.Column(db.Text)

    currency = db.Column(db.Text)
    total_amount = db.Column(db.Float)
    gov_fee = db.Column(db.Float)
    service_fee = db.Column(db.Float)
    vat_amount = db.Column(db.Float)

    received_total = db.Column(db.Float)
    outstanding_amount = db.Column(db.Float)
    status = db.Column(db.Text)
    status_changed_date = db.Column(db.Text)

    description = db.Column(db.Text)
    raw_id = db.Column(db.Text)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)
    deleted_op_id = db.Column(db.Integer, index=True)


class ExternalInvoiceCaseLink(db.Model):
    __tablename__ = "external_invoice_case_link"

    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Text, db.ForeignKey("matter.matter_id"), nullable=False, index=True)
    our_ref = db.Column(db.Text, index=True)

    external_invoice_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    external_invoice_number = db.Column(db.Text, index=True)
    external_invoice_url = db.Column(db.Text)

    ipm_invoice_id = db.Column(db.Text, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)
    deleted_op_id = db.Column(db.Integer, index=True)


class ExternalInvoiceCaseMap(db.Model):
    __tablename__ = "external_invoice_case_map"

    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Text, db.ForeignKey("matter.matter_id"), nullable=False, index=True)
    our_ref = db.Column(db.Text, index=True)

    external_invoice_id = db.Column(db.Integer, nullable=False, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)
    deleted_op_id = db.Column(db.Integer, index=True)

    __table_args__ = (
        db.UniqueConstraint(
            "matter_id", "external_invoice_id", name="uq_external_invoice_case_map"
        ),
    )


class LegacyInvoicePayment(db.Model):
    __tablename__ = "invoice_payment"

    payment_id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    invoice_id = db.Column(db.Text, nullable=False, index=True)
    installment_no = db.Column(db.Integer, nullable=False)
    paid_date = db.Column(db.Text)
    paid_amount = db.Column(db.Float)
    method = db.Column(db.Text)
    payer_name = db.Column(db.Text)
    fx_rate = db.Column(db.Float)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)
    deleted_op_id = db.Column(db.Integer, index=True)


class LegacyExpense(db.Model):
    __tablename__ = "expense"

    expense_id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    matter_id = db.Column(db.Text, nullable=False, index=True)

    expense_ref = db.Column(db.Text)
    dn_no = db.Column(db.Text)
    dn_date = db.Column(db.Text)
    remit_no = db.Column(db.Text)
    expense_date = db.Column(db.Text)
    due_date = db.Column(db.Text)
    vendor_name = db.Column(db.Text)
    category_code = db.Column(db.Text)

    currency = db.Column(db.Text)
    requested_total = db.Column(db.Float)
    remit_total = db.Column(db.Float)
    outstanding_amount = db.Column(db.Float)
    status = db.Column(db.Text)

    description = db.Column(db.Text)
    raw_id = db.Column(db.Text)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)
    deleted_op_id = db.Column(db.Integer, index=True)


class LegacyExpensePayment(db.Model):
    __tablename__ = "expense_payment"

    exp_payment_id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    expense_id = db.Column(db.Text, nullable=False, index=True)
    installment_no = db.Column(db.Integer, nullable=False)
    sent_date = db.Column(db.Text)
    sent_amount = db.Column(db.Float)
    fx_rate = db.Column(db.Float)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)
    deleted_op_id = db.Column(db.Integer, index=True)


class CaseExpenseInvoiceMap(db.Model):
    __tablename__ = "case_expense_invoice_map"

    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Text, nullable=False)
    expense_id = db.Column(db.Text, nullable=False)
    billing_invoice_id = db.Column(db.Integer, nullable=False)
    billing_line_item_id = db.Column(db.Integer)
    amount_minor = db.Column(db.Integer)
    currency = db.Column(db.Text)
    created_by = db.Column(db.Integer)
    created_at = db.Column(db.Text, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    is_deleted = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    deleted_at = db.Column(db.Text)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)

    __table_args__ = (
        db.UniqueConstraint(
            "expense_id",
            "billing_invoice_id",
            "billing_line_item_id",
            name="uq_case_expense_invoice_map",
        ),
        db.Index("idx_ceim_matter", "matter_id"),
        db.Index("idx_ceim_expense", "expense_id"),
        db.Index("idx_ceim_invoice", "billing_invoice_id"),
    )
