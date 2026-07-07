from datetime import datetime

from app.extensions import db


class Folder(db.Model):
    __tablename__ = "folders"

    id = db.Column(db.Integer, primary_key=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("folders.id"))
    name = db.Column(db.String(200), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    is_team = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    parent = db.relationship("Folder", remote_side=[id])
    documents = db.relationship("Document", backref="folder", cascade="all, delete-orphan")


class Document(db.Model):
    __tablename__ = "documents"

    id = db.Column(db.Integer, primary_key=True)
    folder_id = db.Column(db.Integer, db.ForeignKey("folders.id"))
    title = db.Column(db.String(200), nullable=False)
    current_version_id = db.Column(db.Integer)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    versions = db.relationship("DocumentVersion", backref="document", cascade="all, delete-orphan")


class DocumentVersion(db.Model):
    __tablename__ = "document_versions"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False)
    version_no = db.Column(db.Integer, default=1)
    file_path = db.Column(db.String(500), nullable=False)
    size = db.Column(db.Integer, default=0)
    checksum = db.Column(db.String(64))
    uploaded_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    note = db.Column(db.String(200))


class Acl(db.Model):
    __tablename__ = "acls"

    id = db.Column(db.Integer, primary_key=True)
    obj_type = db.Column(db.String(20), nullable=False)  # 'folder' or 'document'
    obj_id = db.Column(db.Integer, nullable=False)
    principal_type = db.Column(db.String(20), nullable=False)  # 'user' or 'role'
    principal_id = db.Column(db.Integer, nullable=False)
    perm = db.Column(db.String(20), nullable=False)  # 'read'/'write'/'admin'
