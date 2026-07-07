"""
CRM Models for Lead, Opportunity, Contact, and Activity management.
"""

from datetime import datetime

from app.extensions import db


class CRMLead(db.Model):
    """
    Represents a potential client or a new business inquiry.
    When converted, a Client is created and linked via converted_client_id.
    """

    __tablename__ = "crm_leads"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    company_name = db.Column(db.String(200))
    email = db.Column(db.String(120))
    phone = db.Column(db.String(50))
    # Status: new, contacted, qualified, converted, lost
    status = db.Column(db.String(20), default="new", nullable=False, index=True)
    # Source: website, referral, cold_call, advertisement, trade_show, etc.
    source = db.Column(db.String(100))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Foreign keys
    assigned_to = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    converted_client_id = db.Column(db.Integer, db.ForeignKey("clients.id"))

    # Relationships
    assignee = db.relationship("User", foreign_keys=[assigned_to], backref="assigned_leads")
    converted_client = db.relationship("Client", foreign_keys=[converted_client_id])
    activities = db.relationship(
        "CRMActivity", backref="lead", lazy="dynamic", foreign_keys="CRMActivity.lead_id"
    )

    # Status choices for form rendering
    STATUS_CHOICES = [
        ("new", ""),
        ("contacted", "Done"),
        ("qualified", "Done"),
        ("converted", "Done"),
        ("lost", ""),
    ]

    # Source choices for form rendering
    SOURCE_CHOICES = [
        ("website", ""),
        ("referral", ""),
        ("cold_call", ""),
        ("advertisement", ""),
        ("trade_show", ""),
        ("other", "Other"),
    ]

    def __repr__(self):
        return f"<CRMLead {self.name}>"


class CRMOpportunity(db.Model):
    """
    Represents a potential deal or case with an existing or new client.
    Used to forecast revenue and manage the sales pipeline.
    """

    __tablename__ = "crm_opportunities"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)  # e.g., "Trademark Application for XYZ"
    # Stage: prospecting, proposal, negotiation, closed_won, closed_lost
    stage = db.Column(db.String(30), default="prospecting", nullable=False, index=True)
    amount = db.Column(db.Numeric(15, 2))  # Expected value
    probability = db.Column(db.Integer, default=10)  # 0-100%
    expected_close_date = db.Column(db.Date)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    closed_at = db.Column(db.DateTime)
    notes = db.Column(db.Text)

    # Relationships
    activities = db.relationship(
        "CRMActivity",
        backref="opportunity",
        lazy="dynamic",
        foreign_keys="CRMActivity.opportunity_id",
    )

    # Stage choices for form rendering
    STAGE_CHOICES = [
        ("prospecting", ""),
        ("proposal", ""),
        ("negotiation", ""),
        ("closed_won", ""),
        ("closed_lost", ""),
    ]

    # Probability defaults by stage
    STAGE_PROBABILITY = {
        "prospecting": 10,
        "proposal": 30,
        "negotiation": 60,
        "closed_won": 100,
        "closed_lost": 0,
    }

    def __repr__(self):
        return f"<CRMOpportunity {self.name}>"


class CRMContact(db.Model):
    """
    Represents an individual person at a Client organization.
    The Client model represents the legal entity; this model represents the humans we talk to.
    """

    __tablename__ = "crm_contacts"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    title = db.Column(db.String(100))  # Job title/position
    email = db.Column(db.String(120))
    phone = db.Column(db.String(50))
    mobile = db.Column(db.String(50))
    is_primary = db.Column(db.Boolean, default=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<CRMContact {self.name}>"


class CRMActivity(db.Model):
    """
    Represents an interaction log (call, email, meeting, note).
    Can be associated with a Client, Lead, or Opportunity.
    """

    __tablename__ = "crm_activities"

    id = db.Column(db.Integer, primary_key=True)
    # One of these should be set (but all are optional for flexibility)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), index=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("crm_leads.id"), index=True)
    opportunity_id = db.Column(db.Integer, db.ForeignKey("crm_opportunities.id"), index=True)

    # Activity type: call, email, meeting, note
    type = db.Column(db.String(20), nullable=False, index=True)
    summary = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    activity_date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Who logged this activity
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    user = db.relationship("User", foreign_keys=[user_id], backref="logged_activities")

    # Type choices for form rendering
    TYPE_CHOICES = [
        ("call", ""),
        ("email", "Email"),
        ("meeting", ""),
        ("note", "Notes"),
    ]

    def __repr__(self):
        return f"<CRMActivity {self.type}: {self.summary[:30]}>"
