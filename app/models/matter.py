import uuid

from sqlalchemy.ext.mutable import MutableDict

from app.extensions import db
from app.utils.timezone import utcnow_naive


class Matter(db.Model):
    """Canonical matter model.

    New matter/case-like features should reference ``Matter.matter_id``. The
    legacy ``Case`` model remains for pre-migration compatibility only.
    """

    __tablename__ = "matter"

    matter_id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    our_ref = db.Column(db.Text, nullable=False, unique=True, index=True)
    old_our_ref = db.Column(db.Text)
    your_ref = db.Column(db.Text)
    right_name = db.Column(db.Text)
    right_group = db.Column(db.Text)
    matter_type = db.Column(db.Text)
    status_red = db.Column(db.Text)
    status_red_related_date = db.Column(db.Text)
    status_red_related_on = db.Column(db.Date)
    status_blue = db.Column(db.Text)
    inhouse_status = db.Column(db.Text)
    memo = db.Column(db.Text)
    retained_at = db.Column(db.Text)
    retained_date = db.Column(db.Date)
    entered_at = db.Column(db.Text)
    entered_date = db.Column(db.Date)
    created_at = db.Column(db.DateTime, default=utcnow_naive, index=True)
    raw_id = db.Column(db.Text)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)
    deleted_op_id = db.Column(db.Integer, index=True)


class VMatterOverview(db.Model):
    __tablename__ = "v_matter_overview"

    matter_id = db.Column(db.Text, primary_key=True)
    our_ref = db.Column(db.Text)
    old_our_ref = db.Column(db.Text)
    your_ref = db.Column(db.Text)
    right_name = db.Column(db.Text)
    right_group = db.Column(db.Text)
    matter_type = db.Column(db.Text)
    status_red = db.Column(db.Text)
    status_blue = db.Column(db.Text)
    inhouse_status = db.Column(db.Text)
    retained_at = db.Column(db.Text)
    entered_at = db.Column(db.Text)
    created_at = db.Column(db.DateTime)
    clients = db.Column(db.Text)
    applicants = db.Column(db.Text)
    attorneys = db.Column(db.Text)
    family_keys = db.Column(db.Text)
    is_family_lead = db.Column(db.Integer)
    next_due_date = db.Column(db.Text)
    next_due_name = db.Column(db.Text)
    open_docket_count = db.Column(db.Integer)
    billed_total = db.Column(db.Float)
    received_total = db.Column(db.Float)
    outstanding_total = db.Column(db.Float)
    exp_requested_total = db.Column(db.Float)
    exp_remit_total = db.Column(db.Float)
    exp_outstanding_total = db.Column(db.Float)


class MatterIdentifier(db.Model):
    __tablename__ = "matter_identifier"

    mid_id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    matter_id = db.Column(db.Text, db.ForeignKey("matter.matter_id"), nullable=False, index=True)
    id_type = db.Column(db.Text, nullable=False)
    id_value = db.Column(db.Text, nullable=False)
    country = db.Column(db.Text)
    raw_text = db.Column(db.Text)
    source_column = db.Column(db.Text)
    raw_id = db.Column(db.Text)


class MatterCustomField(db.Model):
    __tablename__ = "matter_custom_field"

    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Text, nullable=False, index=True)
    namespace = db.Column(db.String(50), nullable=False, index=True)
    data = db.Column(MutableDict.as_mutable(db.JSON), default=dict)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive)

    __table_args__ = (db.UniqueConstraint("matter_id", "namespace", name="uq_matter_custom_field"),)


class MatterEvent(db.Model):
    __tablename__ = "matter_event"

    mevent_id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    matter_id = db.Column(db.Text, db.ForeignKey("matter.matter_id"), nullable=False, index=True)
    event_key = db.Column(db.Text, nullable=False, index=True)
    event_at = db.Column(db.Text)
    event_date = db.Column(db.Date)
    raw_text = db.Column(db.Text)
    source_column = db.Column(db.Text)


class EventKeyMap(db.Model):
    __tablename__ = "event_key_map"

    map_id = db.Column(db.Integer, primary_key=True)
    raw_event_key = db.Column(db.String(100), index=True, nullable=False, unique=True)
    std_event_key = db.Column(db.String(100), index=True, nullable=False)


class MatterPartyRole(db.Model):
    __tablename__ = "matter_party_role"

    mpr_id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    matter_id = db.Column(db.Text, db.ForeignKey("matter.matter_id"), nullable=False, index=True)
    party_id = db.Column(db.Text, index=True)
    role_code = db.Column(db.Text, nullable=False, index=True)
    seq = db.Column(db.Integer, default=1)
    raw_text = db.Column(db.Text)


class MatterStaffAssignment(db.Model):
    __tablename__ = "matter_staff_assignment"

    msa_id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    matter_id = db.Column(db.Text, nullable=False, index=True)
    staff_party_id = db.Column(db.Text, index=True)
    staff_role_code = db.Column(db.Text, nullable=False, index=True)
    seq = db.Column(db.Integer, default=1)
    raw_text = db.Column(db.Text)


class MatterMemo(db.Model):
    __tablename__ = "matter_memo"

    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Text, db.ForeignKey("matter.matter_id"), nullable=False, index=True)
    body = db.Column(db.Text, nullable=False)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_by_name = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=utcnow_naive, index=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id])


class MatterMemoFileAsset(db.Model):
    __tablename__ = "matter_memo_file_asset"

    memo_file_id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    memo_id = db.Column(db.Integer, nullable=False, index=True)
    file_asset_id = db.Column(db.Text, nullable=False, index=True)
    role = db.Column(db.Text, default="")
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow_naive)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)
    deleted_op_id = db.Column(db.Integer, index=True)

    __table_args__ = (
        db.UniqueConstraint("memo_id", "file_asset_id", "role", name="uq_matter_memo_file_asset"),
    )


class MatterProgress(db.Model):
    """Unified progress tracking for a matter (Progress).

    Replaces legacy MatterCustomField entries: progress_misc, progress, old_workflow.
    """

    __tablename__ = "matter_progress"

    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Text, db.ForeignKey("matter.matter_id"), nullable=False, index=True)
    content = db.Column(db.Text, nullable=False)

    # Category to preserve origin context during migration
    # 'general' (Open), 'misc' (Other Open), 'legacy_staff' ( TaskResponsible), 'user' (new entries)
    category = db.Column(db.String(50), default="general")

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_by_name = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=utcnow_naive, index=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id])


class MatterStatusHistory(db.Model):
    """
    Status history for Matter.inhouse_status updates.
    Minimal schema (text dates) to match legacy legacy IPM conventions.
    """

    __tablename__ = "matter_status_history"

    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Text, nullable=False, index=True)
    status = db.Column(db.Text, nullable=False)
    status_date = db.Column(db.Text, nullable=False)  # store YYYY-MM-DD (or raw string)
    note = db.Column(db.Text)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_by_name = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=utcnow_naive, index=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id])


class Family(db.Model):
    __tablename__ = "family"

    family_id = db.Column(db.Text, primary_key=True)
    family_key = db.Column(db.Text, nullable=False, unique=True)
    key_type = db.Column(db.Text)
    key_value = db.Column(db.Text)
    created_at = db.Column(db.Text)


class MatterFamily(db.Model):
    __tablename__ = "matter_family"

    mf_id = db.Column(db.Text, primary_key=True)
    matter_id = db.Column(db.Text, nullable=False, index=True)
    family_id = db.Column(db.Text, nullable=False, index=True)
    link_role = db.Column(db.Text)
    is_lead = db.Column(db.Integer, default=0)
    created_at = db.Column(db.Text)
