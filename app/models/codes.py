from app.extensions import db


class CodeGroup(db.Model):
    __tablename__ = "code_groups"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(30), unique=True, nullable=False)  # e.g., RIGHTS_TYPE, DIVISION
    name = db.Column(db.String(100), nullable=False)


class Code(db.Model):
    __tablename__ = "codes"
    id = db.Column(db.Integer, primary_key=True)
    group_code = db.Column(
        db.String(30), db.ForeignKey("code_groups.code"), index=True, nullable=False
    )
    code = db.Column(db.String(30), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    sort = db.Column(db.Integer, default=0)
    active = db.Column(db.Boolean, default=True)

    __table_args__ = (db.UniqueConstraint("group_code", "code", name="uq_codes_group_code"),)
