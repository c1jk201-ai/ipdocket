from datetime import datetime

from app.extensions import db


class DeletionLog(db.Model):
    __tablename__ = "deletion_logs"

    id = db.Column(db.Integer, primary_key=True)
    entity_type = db.Column(db.String(50), nullable=False)
    entity_id = db.Column(db.Integer, nullable=False)
    entity_key = db.Column(db.Text)
    title = db.Column(db.String(255))
    payload = db.Column(db.JSON)  # backup fields for restore
    parent_type = db.Column(db.String(50), index=True)
    parent_id = db.Column(db.Text, index=True)
    search_vector = db.Column(db.Text)
    tags = db.Column(db.String(255))
    deleted_by = db.Column(db.Integer, index=True)
    deleted_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    restored_entity_id = db.Column(db.Integer)
    restored_entity_key = db.Column(db.Text)
    restored_by = db.Column(db.Integer, index=True)
    restored_at = db.Column(db.DateTime, index=True)

    __table_args__ = (
        db.Index("ix_deletion_logs_entity", "entity_type", "entity_id"),
        db.Index("ix_deletion_logs_parent", "parent_type", "parent_id"),
        db.Index(
            "ix_deletion_logs_restore",
            "restored_at",
            "restored_by",
            "restored_entity_id",
        ),
    )
