from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import UniqueConstraint

from app.extensions import db


class EmailMessage(db.Model):
    __tablename__ = "email_message"
    __table_args__ = (
        db.Index(
            "ix_email_message_inbox_list",
            "mailbox_tag",
            "processing_status",
            "received_at",
            "id",
        ),
    )

    id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    provider_message_id = db.Column(db.Text, index=True, unique=True)
    thread_id = db.Column(db.Text, index=True)
    from_addr = db.Column("from", db.Text)
    to_text = db.Column("to", db.Text)
    cc_text = db.Column("cc", db.Text)
    subject = db.Column(db.Text)
    received_at = db.Column(db.DateTime, index=True)
    ignored_at = db.Column(db.DateTime, index=True)
    body_text = db.Column(db.Text().with_variant(mysql.LONGTEXT, "mysql"))
    body_html = db.Column(db.Text().with_variant(mysql.LONGTEXT, "mysql"))
    raw_eml_path = db.Column(db.Text)
    mailbox_tag = db.Column(db.Text, index=True)
    suggested_matter_id = db.Column(db.Text)
    suggested_score = db.Column(db.Float)
    suggested_reasons = db.Column(db.JSON)
    selected_matter_id = db.Column(db.Text)
    selected_by = db.Column(db.Text)
    linked_comm_id = db.Column(db.Text)
    processing_status = db.Column(db.Text, default="NEW", index=True)


class EmailMessageTombstone(db.Model):
    __tablename__ = "email_message_tombstone"

    provider_message_id = db.Column(db.Text, primary_key=True)
    reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)


class EmailMessageMatterLink(db.Model):
    __tablename__ = "email_message_matter_link"

    id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    email_id = db.Column(db.Text, nullable=False, index=True)
    matter_id = db.Column(db.Text, nullable=False, index=True)
    selected_by = db.Column(db.Text)
    selected_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    comm_id = db.Column(db.Text, index=True, unique=True)

    __table_args__ = (
        UniqueConstraint("email_id", "matter_id", name="uq_email_message_matter_link"),
    )


class EmailAttachment(db.Model):
    __tablename__ = "email_attachment"

    id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    email_id = db.Column(db.Text, nullable=False, index=True)
    filename = db.Column(db.Text)
    mime = db.Column(db.Text)
    size = db.Column(db.Integer)
    sha256 = db.Column(db.String(64), index=True)
    storage_path = db.Column(db.Text)
    extracted_text_path = db.Column(db.Text)
    ocr_text_path = db.Column(db.Text)
    page_count = db.Column(db.Integer)
    is_scanned_prob = db.Column(db.Float)


class IngestionRun(db.Model):
    __tablename__ = "ingestion_run"

    id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    email_id = db.Column(db.Text, nullable=False, index=True)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime)
    status = db.Column(db.Text, index=True)
    model_name = db.Column(db.Text)
    model_version = db.Column(db.Text)
    prompt_version = db.Column(db.Text)
    tokens_in = db.Column(db.Integer)
    tokens_out = db.Column(db.Integer)
    cost_estimate = db.Column(db.Float)
    error_code = db.Column(db.Text)
    error_detail = db.Column(db.Text)


class EmailIngestionLog(db.Model):
    __tablename__ = "email_ingestion_log"

    id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    provider = db.Column(db.Text, index=True)
    mailbox = db.Column(db.Text, index=True)
    cursor_start = db.Column(db.Text)
    cursor_end = db.Column(db.Text)
    fetched_count = db.Column(db.Integer)
    ingested_count = db.Column(db.Integer)
    duplicate_count = db.Column(db.Integer)
    missing_count = db.Column(db.Integer)
    out_of_order_count = db.Column(db.Integer)
    details = db.Column(db.JSON)
    error_code = db.Column(db.Text)
    error_detail = db.Column(db.Text)


class ExtractionResult(db.Model):
    __tablename__ = "extraction_result"

    id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    run_id = db.Column(db.Text, nullable=False, index=True)
    doc_type = db.Column(db.Text)
    jurisdiction = db.Column(db.Text)
    language = db.Column(db.Text)
    structured_json = db.Column(db.JSON)
    overall_confidence = db.Column(db.Float)


class FieldEvidence(db.Model):
    __tablename__ = "field_evidence"

    id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    result_id = db.Column(db.Text, nullable=False, index=True)
    field_path = db.Column(db.Text, nullable=False)
    value = db.Column(db.Text)
    confidence = db.Column(db.Float)
    source = db.Column(db.Text)
    evidence = db.Column(db.JSON)
    normalization_note = db.Column(db.Text)


class MatterMatch(db.Model):
    __tablename__ = "matter_match"

    id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    run_id = db.Column(db.Text, nullable=False, index=True)
    candidate_matter_id = db.Column(db.Text, index=True)
    score = db.Column(db.Float)
    reasons = db.Column(db.JSON)
    chosen = db.Column(db.Boolean, default=False)
    chosen_by = db.Column(db.Text)


class MailMatchCandidate(db.Model):
    __tablename__ = "mail_match_candidate"

    id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    email_id = db.Column(db.Text, nullable=False, index=True)
    candidate_matter_id = db.Column(db.Text, index=True)
    score = db.Column(db.Float)
    reasons = db.Column(db.JSON)
    rank = db.Column(db.Integer)


class AutomationChangeSet(db.Model):
    __tablename__ = "automation_change_set"

    id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    run_id = db.Column(db.Text, nullable=False, index=True)
    matter_id = db.Column(db.Text, nullable=False, index=True)
    param_updates = db.Column(db.JSON, default=dict)
    docket_upserts = db.Column(db.JSON, default=list)
    annuity_upserts = db.Column(db.JSON, default=list)
    applied = db.Column(db.Boolean, default=False)
    applied_at = db.Column(db.DateTime)
    applied_by = db.Column(db.Text)
    rollback_key = db.Column(db.Text)


class AutomationChangeSnapshot(db.Model):
    __tablename__ = "automation_change_snapshot"

    id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    change_set_id = db.Column(db.Text, nullable=False, index=True)
    rollback_key = db.Column(db.Text, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    before_snapshot = db.Column(db.JSON)
    after_snapshot = db.Column(db.JSON)
    diff = db.Column(db.JSON)


class AutomationReviewFeedback(db.Model):
    __tablename__ = "automation_review_feedback"

    id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    run_id = db.Column(db.Text, nullable=False, index=True)
    extraction_result_id = db.Column(db.Text, index=True)
    change_set_id = db.Column(db.Text, index=True)
    matter_id = db.Column(db.Text, index=True)
    doc_type = db.Column(db.Text, index=True)
    action = db.Column(db.Text, nullable=False, index=True)
    label = db.Column(db.Text, nullable=False, index=True)
    reason = db.Column(db.Text)
    reviewer_id = db.Column(db.Text)
    automation_level = db.Column(db.Text)
    confidence_overall = db.Column(db.Float)
    before_json = db.Column(db.JSON)
    after_json = db.Column(db.JSON)
    details = db.Column(db.JSON)


class AutomationFieldFeedback(db.Model):
    __tablename__ = "automation_field_feedback"
    __table_args__ = (db.Index("ix_automation_field_feedback_doc_field", "doc_type", "field_path"),)

    id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    feedback_id = db.Column(db.Text, nullable=False, index=True)
    run_id = db.Column(db.Text, nullable=False, index=True)
    extraction_result_id = db.Column(db.Text, index=True)
    change_set_id = db.Column(db.Text, index=True)
    matter_id = db.Column(db.Text, index=True)
    doc_type = db.Column(db.Text, index=True)
    action = db.Column(db.Text, nullable=False, index=True)
    label = db.Column(db.Text, nullable=False, index=True)
    field_path = db.Column(db.Text, nullable=False, index=True)
    reviewer_id = db.Column(db.Text)
    before_value = db.Column(db.Text)
    after_value = db.Column(db.Text)
    confidence = db.Column(db.Float)
    evidence_present = db.Column(db.Boolean, default=False, nullable=False)
    details = db.Column(db.JSON)
