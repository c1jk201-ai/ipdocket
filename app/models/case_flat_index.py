"""CaseFlatIndex model for denormalized canonical field storage."""

from datetime import datetime

from app.extensions import db


class CaseFlatIndex(db.Model):
    """
    Denormalized flat index table for efficient case search/filter.

    Stores canonical field values (attorney, manager, handler, etc.)
    resolved from matter_custom_field namespaces. Updated on case save/update.
    """

    __tablename__ = "case_flat_index"

    matter_id = db.Column(db.Text, primary_key=True)

    # Staff fields (canonical: attorney, manager, handler)
    attorney = db.Column(db.Text, index=True)
    attorney_id = db.Column(db.Text)
    manager = db.Column(db.Text, index=True)
    manager_id = db.Column(db.Text)
    handler = db.Column(db.Text, index=True)
    handler_id = db.Column(db.Text)
    drawing_contact = db.Column(db.Text)

    # Core identifiers
    application_no = db.Column(db.Text, index=True)
    registration_no = db.Column(db.Text, index=True)
    publication_no = db.Column(db.Text)

    # Key parties
    applicant = db.Column(db.Text, index=True)
    client_name = db.Column(db.Text, index=True)
    inventor = db.Column(db.Text, index=True)

    # Key dates
    application_date = db.Column(db.Text)
    registration_date = db.Column(db.Text)
    priority_date = db.Column(db.Text)

    # Department and status
    department = db.Column(db.Text)
    status_internal = db.Column(db.Text)

    # Case kind metadata
    namespace = db.Column(db.Text)

    # Pre-computed searchable text for efficient compact search
    # Contains concatenated: our_ref, your_ref, clients, right_name, inventor, applicant, etc.
    search_text = db.Column(db.Text)
    # Pre-computed compact representation of search_text for DB-side LIKE search
    search_compact = db.Column(db.Text, index=True)

    # Tracking
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<CaseFlatIndex matter_id={self.matter_id}>"
