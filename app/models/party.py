from app.extensions import db


class Party(db.Model):
    __tablename__ = "party"

    party_id = db.Column(db.Text, primary_key=True)
    name_display = db.Column(db.Text, nullable=False)
    name_en = db.Column(db.Text)
    party_kind = db.Column(db.Text)
    nationality = db.Column(db.Text)
    reg_no = db.Column(db.Text)
    business_no = db.Column(db.Text)
    created_at = db.Column(db.Text)


class PartyStaff(db.Model):
    __tablename__ = "party_staff"

    party_id = db.Column(db.Text, primary_key=True)
    staff_code = db.Column(db.Text)
    dept = db.Column(db.Text)
    active = db.Column(db.Integer)


class PartyCode(db.Model):
    __tablename__ = "party_code"

    party_code_id = db.Column(db.Text, primary_key=True)
    party_id = db.Column(db.Text, nullable=False, index=True)
    code_type = db.Column(db.Text, nullable=False)
    code_value = db.Column(db.Text, nullable=False)


class PartyContact(db.Model):
    __tablename__ = "party_contact"

    contact_id = db.Column(db.Text, primary_key=True)
    party_id = db.Column(db.Text, nullable=False, index=True)
    contact_type = db.Column(db.Text, nullable=False)
    label = db.Column(db.Text)
    value = db.Column(db.Text, nullable=False)


class PartyAddress(db.Model):
    __tablename__ = "party_address"

    address_id = db.Column(db.Text, primary_key=True)
    party_id = db.Column(db.Text, nullable=False, index=True)
    address_type = db.Column(db.Text, nullable=False)
    address_text = db.Column(db.Text, nullable=False)
