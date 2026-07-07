from datetime import datetime

from app.extensions import db


class JobRun(db.Model):
    __tablename__ = "job_runs"

    id = db.Column(db.Integer, primary_key=True)
    job_name = db.Column(db.Text, nullable=False, index=True)
    run_id = db.Column(db.Text, nullable=False, index=True)
    status = db.Column(db.Text, index=True)
    started_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    finished_at = db.Column(db.DateTime)
    request_id = db.Column(db.Text, index=True)
    operation_id = db.Column(db.Integer, db.ForeignKey("operations.id"), index=True)
    input_ref = db.Column(db.Text)
    output_ref = db.Column(db.Text)
    error = db.Column(db.Text)
