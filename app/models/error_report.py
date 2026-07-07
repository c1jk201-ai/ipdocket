from datetime import datetime

from app.extensions import db


class ErrorReport(db.Model):
    __tablename__ = "error_reports"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True, nullable=True)
    method = db.Column(db.String(10))
    path = db.Column(db.Text)
    query_string = db.Column(db.Text)
    endpoint = db.Column(db.String(255))
    blueprint = db.Column(db.String(255))
    remote_addr = db.Column(db.String(255))
    user_agent = db.Column(db.String(512))
    status_code = db.Column(db.Integer, index=True)
    request_id = db.Column(db.Text, index=True)
    matter_id = db.Column(db.Text, index=True)
    invoice_id = db.Column(db.Text, index=True)
    workflow_id = db.Column(db.Text, index=True)
    error_type = db.Column(db.String(255))
    message = db.Column(db.Text)
    traceback = db.Column(db.Text)

    user = db.relationship("User", foreign_keys=[user_id])
