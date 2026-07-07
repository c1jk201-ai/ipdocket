import uuid

from app.extensions import db


class RawImportField(db.Model):
    __tablename__ = "raw_import_field"

    raw_field_id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    raw_id = db.Column(db.Text, nullable=False, index=True)
    sheet_name = db.Column(db.Text, nullable=False, index=True)
    source_column = db.Column(db.Text, nullable=False)
    value_text = db.Column(db.Text)
    created_at = db.Column(db.Text)

    __table_args__ = (db.UniqueConstraint("raw_id", "source_column", name="uq_raw_import_field"),)
