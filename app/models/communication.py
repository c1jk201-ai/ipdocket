import uuid

from sqlalchemy.orm import validates

from app.extensions import db
from app.utils.search import compact_search_text as to_compact_compact
from app.utils.timezone import utcnow_iso


class Communication(db.Model):
    __tablename__ = "communication"

    comm_id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    matter_id = db.Column(db.Text, nullable=False, index=True)
    comm_type = db.Column(db.Text)
    to_text = db.Column(db.Text)
    body = db.Column(db.Text)
    received_date = db.Column(db.Text)
    sent_date = db.Column(db.Text)
    due_date = db.Column(db.Text)
    done_date = db.Column(db.Text)
    author_staff_party_id = db.Column(db.Text)
    owner_staff_party_id = db.Column(db.Text)
    mail_no = db.Column(db.Text)
    letter_no = db.Column(db.Text)
    note = db.Column(db.Text)
    # For fast  Search (DB-side LIKE on precomputed compact)
    search_compact = db.Column(db.Text, index=True)
    raw_id = db.Column(db.Text)

    @validates("note")
    def _sync_search_compact(self, key, value):
        v = value or ""
        self.search_compact = to_compact_compact(v) if v else None
        return value


class CommunicationFileAsset(db.Model):
    __tablename__ = "communication_file_asset"

    comm_file_id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    comm_id = db.Column(db.Text, nullable=False, index=True)
    file_asset_id = db.Column(db.Text, nullable=False, index=True)
    role = db.Column(db.Text, default="")
    description = db.Column(db.Text)
    created_at = db.Column(db.Text, default=utcnow_iso)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)
    deleted_op_id = db.Column(db.Integer, index=True)

    __table_args__ = (
        db.UniqueConstraint("comm_id", "file_asset_id", "role", name="uq_comm_file_asset"),
    )


class OfficeAction(db.Model):
    __tablename__ = "office_action"

    oa_id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    matter_id = db.Column(db.Text, nullable=False, index=True)
    doc_name = db.Column(db.Text)
    # For fast  Search (DB-side LIKE on precomputed compact)
    search_compact = db.Column(db.Text, index=True)
    received_date = db.Column(db.Text)
    notified_date = db.Column(db.Text)
    due_date = db.Column(db.Text)
    extended_due_date = db.Column(db.Text)
    done_date = db.Column(db.Text)
    examiner = db.Column(db.Text)
    review_comment = db.Column(db.Text)
    comment_due_date = db.Column(db.Text)
    comment_sent_date = db.Column(db.Text)
    client_report_date = db.Column(db.Text)
    raw_id = db.Column(db.Text)

    @validates("doc_name")
    def _sync_search_compact(self, key, value):
        v = value or ""
        self.search_compact = to_compact_compact(v) if v else None
        return value


class OfficeActionFileAsset(db.Model):
    __tablename__ = "office_action_file_asset"

    oa_file_id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    oa_id = db.Column(db.Text, nullable=False, index=True)
    file_asset_id = db.Column(db.Text, nullable=False, index=True)
    role = db.Column(db.Text, default="")
    description = db.Column(db.Text)
    created_at = db.Column(db.Text, default=utcnow_iso)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)
    deleted_op_id = db.Column(db.Integer, index=True)

    __table_args__ = (
        db.UniqueConstraint("oa_id", "file_asset_id", "role", name="uq_oa_file_asset"),
    )
