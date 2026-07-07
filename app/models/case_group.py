from app.extensions import db

case_group_map = db.Table(
    "case_group_map",
    db.Column("case_id", db.Integer, db.ForeignKey("cases.id"), primary_key=True),
    db.Column("group_id", db.Integer, db.ForeignKey("case_groups.id"), primary_key=True),
)


class CaseGroup(db.Model):
    __tablename__ = "case_groups"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    note = db.Column(db.String(200))
