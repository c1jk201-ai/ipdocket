import uuid

from sqlalchemy.orm import validates

from app.extensions import db
from app.utils.docket_dates import normalize_date_str, parse_date
from app.utils.error_logging import report_swallowed_exception


class AnnuityItem(db.Model):
    __tablename__ = "annuity_item"
    STATUS_PENDING = "pending"
    STATUS_PAID = "paid"
    STATUS_GIVEUP = "giveup"
    STATUSES = frozenset({STATUS_PENDING, STATUS_PAID, STATUS_GIVEUP})

    annuity_id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    matter_id = db.Column(db.Text, nullable=False)
    owner_staff_party_id = db.Column(db.Text)
    cycle_no = db.Column(db.Integer, nullable=False)
    annuity_status = db.Column(db.Text, default=STATUS_PENDING)
    due_date = db.Column(db.Text)
    extended_due_date = db.Column(db.Text)
    renewal_open_date = db.Column(db.Text)
    renewal_notice_due = db.Column(db.Text)
    discount_rate = db.Column(db.Float)
    official_fee = db.Column(db.Float)
    vat_amount = db.Column(db.Float)
    service_fee = db.Column(db.Float)
    paid_date = db.Column(db.Text)
    paid_amount = db.Column(db.Float)
    internal_due_date = db.Column(db.Text)  # Internal work deadline (Task Due date)
    memo = db.Column(db.Text)
    raw_id = db.Column(db.Text)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)
    deleted_op_id = db.Column(db.Integer, index=True)

    __table_args__ = (db.UniqueConstraint("matter_id", "cycle_no", name="uq_annuity_matter_cycle"),)

    @validates("annuity_status")
    def _validate_annuity_status(self, key, value):
        raw = (value or "").strip()
        if not raw:
            return self.STATUS_PENDING

        v = raw.lower()

        # Give-up / Abandoned (localized + English synonyms)
        if v in ("giveup", "give_up", "give-up", "abandoned", "waived", "forfeit"):
            return self.STATUS_GIVEUP
        if "Abandoned" in raw or "Withdrawn" in raw:
            return self.STATUS_GIVEUP

        # Paid / Completed (localized + English synonyms)
        if v in ("paid", "payed", "done", "complete", "completed"):
            return self.STATUS_PAID
        # Guard: "Done" should not be interpreted as paid.
        if "Done" in raw or " Done" in raw:
            return self.STATUS_PENDING
        if "Receipt" in raw:
            return self.STATUS_PAID
        # M-3 fix: "Payment" + "Done"    items   dead code.

        # overdue Save ( Status). Unknown values are treated as pending (fail-safe).
        return self.STATUS_PENDING

    _DATE_FIELDS = (
        "due_date",
        "extended_due_date",
        "renewal_open_date",
        "renewal_notice_due",
        "paid_date",
        "internal_due_date",
    )

    @validates(*_DATE_FIELDS)
    def _normalize_date_fields(self, key, value):
        return normalize_date_str(value)

    @validates("cycle_no")
    def _normalize_cycle_no(self, key, value):
        if value is None:
            raise ValueError("cycle_no is required")
        if isinstance(value, int):
            if value <= 0:
                raise ValueError("cycle_no must be a positive integer")
            return value
        s = str(value).strip()
        if not s:
            raise ValueError("cycle_no is required")
        try:
            parsed = int(s)
        except Exception as exc:
            parse_error = str(exc)
            try:
                from app.services.automation.parse_failure import record_parse_failure

                record_parse_failure(
                    kind="int",
                    raw_value=s,
                    error=parse_error,
                    source="AnnuityItem._normalize_cycle_no",
                    field_name="cycle_no",
                    entity_type="annuity_item",
                    entity_id=getattr(self, "annuity_id", None),
                    extra={"matter_id": getattr(self, "matter_id", None)},
                )
            except Exception as record_exc:
                report_swallowed_exception(
                    record_exc,
                    context="AnnuityItem._normalize_cycle_no.record_parse_failure",
                    log_key="annuity.normalize_cycle_no",
                    log_window_seconds=300,
                )
            raise ValueError("cycle_no must be a positive integer") from exc
        if parsed <= 0:
            try:
                from app.services.automation.parse_failure import record_parse_failure

                record_parse_failure(
                    kind="int",
                    raw_value=s,
                    error="non_positive",
                    source="AnnuityItem._normalize_cycle_no",
                    field_name="cycle_no",
                    entity_type="annuity_item",
                    entity_id=getattr(self, "annuity_id", None),
                    extra={"matter_id": getattr(self, "matter_id", None)},
                )
            except Exception as record_exc:
                report_swallowed_exception(
                    record_exc,
                    context="AnnuityItem._normalize_cycle_no.record_parse_failure",
                    log_key="annuity.normalize_cycle_no",
                    log_window_seconds=300,
                )
            raise ValueError("cycle_no must be a positive integer")
        return parsed

    @property
    def is_paid(self) -> bool:
        return (
            parse_date(self.paid_date) is not None
            or (self.annuity_status or "").strip().lower() == "paid"
        )
