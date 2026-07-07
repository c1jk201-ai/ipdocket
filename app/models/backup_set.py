from datetime import datetime

from app.extensions import db


class BackupSet(db.Model):
    __tablename__ = "backup_sets"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    type = db.Column(db.Text, nullable=False, index=True)
    reason = db.Column(db.Text)
    git_commit = db.Column(db.Text)
    db_revision = db.Column(db.Text)
    artifact_paths_json = db.Column(db.JSON)
    hashes_json = db.Column(db.JSON)
    verify_status = db.Column(db.Text)
    verify_log = db.Column(db.Text)
    retention_until = db.Column(db.DateTime, index=True)
