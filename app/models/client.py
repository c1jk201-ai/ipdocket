from app.extensions import db


class Client(db.Model):
    __tablename__ = "clients"

    id = db.Column(db.Integer, primary_key=True)
    # Optional linkage to ipm.party.party_id (TEXT PK)
    party_id = db.Column(db.Text, index=True, unique=True, nullable=True)
    # Soft delete fields - prevents party sync from recreating deleted clients
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    deleted_at = db.Column(db.DateTime, nullable=True)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)
    deleted_op_id = db.Column(db.Integer, index=True)
    external_invoice_client_id = db.Column(db.Integer, index=True, unique=True, nullable=True)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(20))  # individual, corporate, foreign_agent
    registration_number = db.Column(db.String(50))  # Business or Resident ID
    contact_person = db.Column(db.String(50))
    manager = db.Column(db.Text)
    email = db.Column(db.String(120))
    phone = db.Column(db.Text)  # Changed from String(20) to Text for longer values
    address = db.Column(db.String(200))
    notes = db.Column(db.Text)
    search_tags = db.Column(db.Text)

    ipm_party_id = db.Column(db.Text, index=True, unique=True, nullable=True)
    ipm_client_id = db.Column(db.Integer, index=True, unique=True, nullable=True)

    biz_reg_number = db.Column(db.Text)
    biz_company_name = db.Column(db.Text)
    biz_representative_name = db.Column(db.Text)
    biz_opening_date = db.Column(db.Text)
    biz_corp_registration_number = db.Column(db.Text)
    biz_business_location = db.Column(db.Text)
    biz_head_office_location = db.Column(db.Text)
    biz_business_type = db.Column(db.Text)
    biz_tax_invoice_email = db.Column(db.Text)
    # Extended fields container
    extra = db.Column(db.JSON)

    cases = db.relationship("Case", backref="client", lazy="dynamic")

    # CRM relationships
    contacts = db.relationship("CRMContact", backref="client", lazy="dynamic")
    opportunities = db.relationship("CRMOpportunity", backref="client", lazy="dynamic")
    activities = db.relationship(
        "CRMActivity", backref="client", lazy="dynamic", foreign_keys="CRMActivity.client_id"
    )

    def __repr__(self):
        return f"<Client {self.name}>"
