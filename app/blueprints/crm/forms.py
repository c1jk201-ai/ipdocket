"""
CRM Forms for Lead, Opportunity, Contact, and Activity management.
"""

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateField,
    DateTimeField,
    DecimalField,
    HiddenField,
    IntegerField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Email, Length, NumberRange, Optional


class LeadForm(FlaskForm):
    """Form for creating/editing a Lead."""

    name = StringField("Name", validators=[DataRequired(), Length(max=100)])
    company_name = StringField("Company", validators=[Optional(), Length(max=200)])
    email = StringField("Email", validators=[Optional(), Email(), Length(max=120)])
    phone = StringField("Phone", validators=[Optional(), Length(max=50)])
    status = SelectField(
        "Status",
        choices=[
            ("new", ""),
            ("contacted", "Done"),
            ("qualified", "Done"),
            ("converted", "Done"),
            ("lost", ""),
        ],
        default="new",
    )
    source = SelectField(
        "",
        choices=[
            ("", "Select"),
            ("website", ""),
            ("referral", ""),
            ("cold_call", ""),
            ("advertisement", ""),
            ("trade_show", ""),
            ("other", "Other"),
        ],
        validators=[Optional()],
    )
    assigned_to = SelectField("Contact", coerce=int, validators=[Optional()])
    notes = TextAreaField("Notes", validators=[Optional()])
    submit = SubmitField("Save")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # assigned_to choices should be set in the view with users list


class OpportunityForm(FlaskForm):
    """Form for creating/editing an Opportunity."""

    client_id = SelectField("Client", coerce=int, validators=[DataRequired()])
    name = StringField("", validators=[DataRequired(), Length(max=200)])
    stage = SelectField(
        "",
        choices=[
            ("prospecting", ""),
            ("proposal", ""),
            ("negotiation", ""),
            ("closed_won", ""),
            ("closed_lost", ""),
        ],
        default="prospecting",
    )
    amount = DecimalField("EstimatedAmount", validators=[Optional()], places=2)
    probability = IntegerField(
        " (%)", validators=[Optional(), NumberRange(min=0, max=100)], default=10
    )
    expected_close_date = DateField("EstimatedDue date", validators=[Optional()], format="%Y-%m-%d")
    notes = TextAreaField("Notes", validators=[Optional()])
    submit = SubmitField("Save")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # client_id choices should be set in the view with clients list


class ContactForm(FlaskForm):
    """Form for creating/editing a Contact."""

    client_id = HiddenField("Client ID", validators=[DataRequired()])
    name = StringField("Name", validators=[DataRequired(), Length(max=100)])
    title = StringField("", validators=[Optional(), Length(max=100)])
    email = StringField("Email", validators=[Optional(), Email(), Length(max=120)])
    phone = StringField("Phone", validators=[Optional(), Length(max=50)])
    mobile = StringField("", validators=[Optional(), Length(max=50)])
    is_primary = BooleanField("Contact")
    notes = TextAreaField("Notes", validators=[Optional()])
    submit = SubmitField("Save")


class ActivityForm(FlaskForm):
    """Form for logging an Activity."""

    # At least one of these should be set
    client_id = HiddenField("Client ID", validators=[Optional()])
    lead_id = HiddenField(" ID", validators=[Optional()])
    opportunity_id = HiddenField(" ID", validators=[Optional()])

    type = SelectField(
        "Type",
        choices=[
            ("call", ""),
            ("email", "Email"),
            ("meeting", ""),
            ("note", "Notes"),
        ],
        validators=[DataRequired()],
    )
    summary = StringField("", validators=[DataRequired(), Length(max=200)])
    description = TextAreaField("DetailsContent", validators=[Optional()])
    activity_date = DateTimeField("", validators=[Optional()], format="%Y-%m-%dT%H:%M")
    submit = SubmitField("Save")


class ClientForm(FlaskForm):
    """Extended client form (for reference - main form defined elsewhere)."""

    idempotency_key = HiddenField("Idempotency Key")
    # Basic info
    client_name = StringField("Client name", validators=[DataRequired(), Length(max=100)])
    name_en = StringField("Client name ()", validators=[Optional(), Length(max=100)])
    input_date = DateField("Entry date", validators=[Optional()], format="%Y-%m-%d")
    category = SelectField(
        "ClientType",
        choices=[
            ("", "Select"),
            ("company", ""),
            ("individual", "items"),
            ("foreign", "Foreign/Foreign"),
        ],
        validators=[Optional()],
    )
    nationality = StringField("", validators=[Optional(), Length(max=50)])
    business_type = SelectField(
        "Type",
        choices=[
            ("", "Select"),
            ("patent", "Patent"),
            ("trademark", "Trademark"),
            ("design", "Design"),
            ("other", "Other"),
        ],
        validators=[Optional()],
    )
    registration_number = StringField("Registration No.", validators=[Optional(), Length(max=50)])

    # Applicant codes
    applicant_code1 = StringField("Applicant 1", validators=[Optional()])
    applicant_code2 = StringField("Applicant 2", validators=[Optional()])
    applicant_code3 = StringField("Applicant 3", validators=[Optional()])
    viewer_default = StringField("Client DefaultSettings", validators=[Optional()])
    annuity_management_disabled = BooleanField("Renewal  ")

    # Contact info
    email = StringField("Email", validators=[Optional(), Email()])
    main_phone = StringField("table ", validators=[Optional()])
    mobile_phone = StringField("", validators=[Optional()])
    main_fax = StringField("Main fax", validators=[Optional()])
    homepage = StringField("Website", validators=[Optional()])
    other_contact = StringField("Other Phone", validators=[Optional()])

    # ------------------------------------------------------------------
    # Shared fields with Accounting/Invoice module (clients table columns)
    # ------------------------------------------------------------------
    manager = StringField("Billing contact", validators=[Optional(), Length(max=200)])
    address = StringField("Default billing address", validators=[Optional(), Length(max=200)])
    notes = TextAreaField("Billing notes", validators=[Optional()])

    # Applicant info
    applicant_address = StringField("Applicant Address", validators=[Optional()])
    applicant_email = StringField("Applicant Email", validators=[Optional(), Email()])
    applicant_phone = StringField("Applicant phone", validators=[Optional()])
    applicant_fax = StringField("Applicant FAX", validators=[Optional()])

    # Business tax profile
    business_reg_no = StringField("Tax ID / EIN", validators=[Optional()])
    tax_company_name = StringField("Legal business name", validators=[Optional()])
    tax_ceo = StringField("Authorized representative", validators=[Optional()])
    tax_business_type = StringField("Entity type", validators=[Optional()])
    tax_business_item = StringField("Business activity", validators=[Optional()])
    tax_address = StringField("Business address", validators=[Optional()])
    tax_manager = StringField("Billing contact", validators=[Optional()])
    tax_manager_email = StringField("Billing contact email", validators=[Optional(), Email()])
    tax_manager_mobile = StringField("Billing contact phone", validators=[Optional()])

    # Mailing info
    mail_recv_address = StringField("Mailing address", validators=[Optional()])
    mail_receiver = StringField("Mail recipient", validators=[Optional()])
    client_code = StringField("Client code", validators=[Optional()])

    # Personal contact
    personal_email = StringField("Personal email", validators=[Optional(), Email()])
    personal_phone = StringField("Personal phone", validators=[Optional()])
    personal_fax = StringField("Personal fax", validators=[Optional()])

    # Other address
    other_address = StringField("Other Address", validators=[Optional()])
    other_email = StringField("Other Email", validators=[Optional(), Email()])
    other_phone = StringField("Other phone", validators=[Optional()])
    other_fax = StringField("Other FAX", validators=[Optional()])

    # Notes
    note = TextAreaField("Internal notes", validators=[Optional()])
    special_note = TextAreaField("Special notes", validators=[Optional()])

    submit = SubmitField("Save")
