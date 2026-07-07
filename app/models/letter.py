from datetime import date, datetime

from app.extensions import db


class Letter(db.Model):
    __tablename__ = "letters"

    id = db.Column(db.Integer, primary_key=True)
    direction = db.Column(db.String(10), default="out")  # in/out
    case_id = db.Column(db.Integer, db.ForeignKey("cases.id"))
    title = db.Column(db.String(200))
    correspondent = db.Column(db.String(100))  # Sender or Receiver
    method = db.Column(db.String(50))  # Email, Fax, Post
    date_sent_received = db.Column(db.Date, default=date.today)
    tracking_no = db.Column(db.String(100))
    status = db.Column(db.String(20), default="sent")  # sent, delivered, failed
    file_path = db.Column(db.String(500))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    created_by = db.relationship("User", foreign_keys=[created_by_id])
