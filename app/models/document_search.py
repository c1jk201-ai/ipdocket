from __future__ import annotations

from datetime import datetime

from sqlalchemy.dialects import mysql

from app.extensions import db


class DocumentSearchIndex(db.Model):
    __tablename__ = "document_search_index"
    __table_args__ = (
        db.UniqueConstraint(
            "source_type",
            "source_id",
            "matter_id",
            name="uq_document_search_index_source",
        ),
        db.Index("ix_document_search_index_matter_source", "matter_id", "source_type"),
    )

    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Text, db.ForeignKey("matter.matter_id"), nullable=False, index=True)
    source_type = db.Column(db.String(40), nullable=False, index=True)
    source_id = db.Column(db.Text, nullable=False, index=True)
    file_asset_id = db.Column(db.Text, index=True)
    title = db.Column(db.Text)
    body = db.Column(db.Text().with_variant(mysql.LONGTEXT, "mysql"))
    mime_type = db.Column(db.Text)
    source_date = db.Column(db.DateTime, index=True)
    url = db.Column(db.Text)
    acl_scope = db.Column(db.String(20), nullable=False, default="matter", server_default="matter")
    indexed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        index=True,
    )
