from app.extensions import db


class FileAsset(db.Model):
    __tablename__ = "file_asset"

    file_asset_id = db.Column(db.Text, primary_key=True)
    storage_type = db.Column(db.Text)
    file_path = db.Column(db.Text)
    original_name = db.Column(db.Text)
    sha256 = db.Column(db.Text, unique=True, index=True)
    byte_size = db.Column(db.Integer)
    mime_type = db.Column(db.Text)
    created_at = db.Column(db.Text)
    virus_scan_status = db.Column(
        db.Text,
        nullable=False,
        default="disabled",
        server_default="disabled",
    )
    virus_scan_checked_at = db.Column(db.DateTime)
    virus_scan_error = db.Column(db.Text)
    quarantined_at = db.Column(db.DateTime)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)
    deleted_op_id = db.Column(db.Integer, index=True)


class MatterFileAsset(db.Model):
    __tablename__ = "matter_file_asset"
    __table_args__ = (
        db.UniqueConstraint("matter_id", "file_asset_id", "role", name="uq_matter_file_asset"),
    )

    matter_file_id = db.Column(db.Text, primary_key=True)
    matter_id = db.Column(db.Text, nullable=False, index=True)
    file_asset_id = db.Column(db.Text, nullable=False, index=True)
    role = db.Column(db.Text)
    description = db.Column(db.Text)
    parent_id = db.Column(db.Text, index=True)
    doc_type = db.Column(db.String(40), index=True)
    tags = db.Column(db.JSON)
    previewable = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.Text)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)
    deleted_op_id = db.Column(db.Integer, index=True)
