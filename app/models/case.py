from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import foreign

from app.extensions import db
from app.models.case_group import CaseGroup, case_group_map
from app.models.matter import Matter


class Case(db.Model):
    """Legacy case table.

    New matter-centric features should use ``Matter.matter_id``. This model is
    retained for older Deadline/RenewalFee/Letter screens and import paths.
    """

    __tablename__ = "cases"

    id = db.Column(db.Integer, primary_key=True)
    # Polymorphic discriminator for joined-table inheritance
    case_type = db.Column(db.String(20))  # e.g., PATENT, DESIGN, TRADEMARK, LITIGATION
    ref_no = db.Column(db.String(50), unique=True, index=True, nullable=False)
    app_no = db.Column(db.String(50), index=True)
    title = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(50))  # Pending, Registered, Abandoned, etc.
    filing_date = db.Column(db.Date)
    division = db.Column(db.String(20))

    # Case Type: Patent, Design, Trademark
    right_type = db.Column(db.String(50))
    # Country: US, JP, etc.
    country = db.Column(db.String(10), default="US")

    # Relationships
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"))
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    manager = db.relationship("User", foreign_keys=[manager_id])
    attorney_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    attorney = db.relationship("User", foreign_keys=[attorney_id])

    # Self-customizing fields (JSON)
    extended_info = db.Column(db.JSON)

    workflows = db.relationship(
        "Workflow",
        secondary=Matter.__table__,
        primaryjoin=or_(
            ref_no == foreign(Matter.our_ref),
            ref_no == foreign(Matter.old_our_ref),
            ref_no == foreign(Matter.your_ref),
        ),
        secondaryjoin="Matter.matter_id == foreign(Workflow.case_id)",
        lazy="dynamic",
        viewonly=True,
    )
    deadlines = db.relationship(
        "Deadline", backref="case", lazy="select", cascade="all, delete-orphan"
    )
    renewal_fees = db.relationship(
        "RenewalFee", backref="case", lazy="select", cascade="all, delete-orphan"
    )
    invoices = db.relationship(
        "Invoice", backref="case", lazy="select", cascade="all, delete-orphan"
    )
    letters = db.relationship("Letter", backref="case", lazy="select", cascade="all, delete-orphan")
    groups = db.relationship(CaseGroup, secondary=case_group_map, backref="cases")

    is_deleted = db.Column(db.Boolean, default=False, nullable=False, server_default="false")
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, index=True)
    delete_reason = db.Column(db.Text)
    deleted_op_id = db.Column(db.Integer, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __mapper_args__ = {
        "polymorphic_on": case_type,
        "polymorphic_identity": "BASE",
    }

    @property
    def rights_type(self):
        return self.right_type

    @rights_type.setter
    def rights_type(self, value):
        self.right_type = value

    @property
    def custom_data(self):
        return self.extended_info

    @custom_data.setter
    def custom_data(self, value):
        self.extended_info = value

    @property
    def app_date(self):
        return self.filing_date

    @app_date.setter
    def app_date(self, value):
        self.filing_date = value

    def __repr__(self):
        return f"<Case {self.ref_no}>"
