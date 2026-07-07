from flask_wtf import FlaskForm
from wtforms import DateField, HiddenField, SelectField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional, Regexp


class CaseForm(FlaskForm):
    idempotency_key = HiddenField()
    our_ref = StringField(
        "Our Ref.",
        validators=[
            DataRequired(),
            Length(min=8, max=50),
            Regexp(
                r"^\d{2}[A-Z]{2}\d{4}[A-Z]{2}$",
                message="Format: YYSS0000CC (for example, 24PD0001US)",
            ),
        ],
        render_kw={"placeholder": "Ex. 24PD0001US"},
    )
    client_ref = StringField("Client Ref.", validators=[Optional(), Length(max=50)])

    category = SelectField(
        "IP Type",
        choices=[
            ("PATENT", "Patent"),
            ("UTILITY", "Utility"),
            ("DESIGN", "Design"),
            ("TRADEMARK", "Trademark"),
            ("LITIGATION", "Proceedings / Litigation"),
            ("MISC", "Other"),
        ],
        validators=[DataRequired()],
        default="PATENT",
    )

    country = SelectField(
        "Country",
        choices=[
            ("US", "United States"),
            ("JP", "Japan"),
            ("CN", "China"),
            ("EP", "Europe"),
        ],
        default="US",
    )

    in_out_type = SelectField(
        "Matter Flow",
        choices=[("DOM", "US"), ("INC", "Inbound US"), ("OUT", "Foreign")],
        default="DOM",
    )

    filing_date = DateField("Application date", format="%Y-%m-%d", validators=[Optional()])
    filing_no = StringField("Application no.", validators=[Optional()])

    reg_date = DateField("Registration date", format="%Y-%m-%d", validators=[Optional()])
    reg_no = StringField("Registration no.", validators=[Optional()])

    client_name = StringField("Client", validators=[DataRequired()])
    client_id = StringField("Client ID", validators=[DataRequired()])

    attorney_id = SelectField("Responsible attorney", choices=[], coerce=int, validators=[DataRequired()])
    manager_id = SelectField("Docketing owner", choices=[], coerce=int, validators=[DataRequired()])

    title = StringField("Matter title", validators=[DataRequired()])
    summary = TextAreaField("Summary / notes", render_kw={"rows": 3})

    submit = SubmitField("Create matter")
