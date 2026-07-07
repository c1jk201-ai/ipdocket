import uuid

from sqlalchemy.orm import validates

from app.extensions import db
from app.utils.docket_dates import normalize_date_str, normalize_done_date


class DocketItem(db.Model):
    __tablename__ = "docket_item"
    _effective_due_expr = (
        "COALESCE(NULLIF(TRIM(extended_due_date), ''), NULLIF(TRIM(due_date), ''))"
    )
    _dashboard_due_expr = f"substr({_effective_due_expr}, 1, 10)"
    _open_dashboard_where = (
        "COALESCE(is_deleted, false) = false "
        "AND (done_date IS NULL OR TRIM(done_date) = '') "
        f"AND {_dashboard_due_expr} IS NOT NULL"
    )
    _done_dashboard_where = (
        "COALESCE(is_deleted, false) = false "
        "AND done_date IS NOT NULL "
        "AND TRIM(done_date) <> '' "
        "AND UPPER(TRIM(done_date)) NOT LIKE 'AUTO_%'"
    )
    __table_args__ = (
        db.Index(
            "ux_docket_item_open_natural",
            "matter_id",
            "name_ref",
            db.text(_effective_due_expr),
            unique=True,
            postgresql_where=db.text(
                "done_date IS NULL "
                "AND COALESCE(is_deleted, false) = false "
                "AND name_ref IS NOT NULL "
                f"AND {_effective_due_expr} IS NOT NULL"
            ),
        ),
        db.Index(
            "ix_docket_item_open_category_due_dashboard",
            "category",
            db.text(_dashboard_due_expr),
            "matter_id",
            "owner_staff_party_id",
            postgresql_where=db.text(_open_dashboard_where),
        ),
        db.Index(
            "ix_docket_item_open_owner_category_due_dashboard",
            "owner_staff_party_id",
            "category",
            db.text(_dashboard_due_expr),
            "matter_id",
            postgresql_where=db.text(_open_dashboard_where),
        ),
        db.Index(
            "ix_docket_item_done_date_dashboard",
            db.text("substr(TRIM(done_date), 1, 10)"),
            postgresql_where=db.text(_done_dashboard_where),
        ),
    )

    docket_id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    matter_id = db.Column(db.Text, nullable=False)
    category = db.Column(db.Text, nullable=False)
    name_ref = db.Column(db.Text)
    name_free = db.Column(db.Text)
    due_date = db.Column(db.Text)
    extended_due_date = db.Column(db.Text)
    visible_from_date = db.Column(db.Text)
    done_date = db.Column(db.Text)
    owner_staff_party_id = db.Column(db.Text)
    snapshot_attorney = db.Column(db.Text)
    snapshot_handler = db.Column(db.Text)
    snapshot_manager = db.Column(db.Text)
    memo = db.Column(db.Text)
    raw_id = db.Column(db.Text)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)
    deleted_op_id = db.Column(db.Integer, index=True)

    @validates("due_date", "extended_due_date", "visible_from_date")
    def _normalize_date_fields(self, key, value):
        return normalize_date_str(value)

    @validates("done_date")
    def _normalize_done_date(self, key, value):
        return normalize_done_date(value)
