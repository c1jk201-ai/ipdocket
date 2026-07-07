from app.extensions import db
from app.models.case import Case

# Note: Using db.JSON for development (SQLite). In production with PostgreSQL, switch to JSONB and add GIN indexes where needed.


class CasePatent(Case):
    __tablename__ = "case_patents"
    id = db.Column(db.Integer, db.ForeignKey("cases.id"), primary_key=True)

    app_type = db.Column(db.String(30))  # ,  
    grade = db.Column(db.String(5))  # S/A/B...

    exam_req_yn = db.Column(db.Boolean, default=False)
    exam_req_date = db.Column(db.Date)
    pub_date = db.Column(db.Date)
    pub_no = db.Column(db.String(50))

    claims_total = db.Column(db.Integer)
    claims_indep = db.Column(db.Integer)
    page_count = db.Column(db.Integer)

    # e.g. [{"country":"US","date":"2024-01-01","no":"12/345678"}]
    # e.g. [{"country":"US","date":"2024-01-01","no":"12/345678"}]
    priority_info = db.Column(db.JSON)

    # Added for Incoming/General support
    app_route = db.Column(db.String(30))  # PCT, EP, etc.
    reg_date = db.Column(db.Date)
    reg_no = db.Column(db.String(50))
    original_app_date = db.Column(db.Date)
    original_app_no = db.Column(db.String(50))
    claims_dep = db.Column(db.Integer)

    __mapper_args__ = {"polymorphic_identity": "PATENT"}


class CaseDesign(Case):
    __tablename__ = "case_designs"
    id = db.Column(db.Integer, db.ForeignKey("cases.id"), primary_key=True)

    exam_type = db.Column(db.String(30))
    is_partial = db.Column(db.Boolean)
    article_name = db.Column(db.String(200))
    image_path = db.Column(db.String(255))

    drawing_count = db.Column(db.Integer)

    # Priority
    priority_claim_yn = db.Column(db.Boolean)
    priority_date = db.Column(db.Date)
    priority_no = db.Column(db.String(50))

    related_app_no = db.Column(db.String(50))  # Related applications (Design )

    app_route = db.Column(db.String(30))  # Hague, Paris
    original_app_date = db.Column(db.Date)
    original_app_no = db.Column(db.String(50))

    pub_date = db.Column(db.Date)
    pub_no = db.Column(db.String(50))
    reg_date = db.Column(db.Date)
    reg_no = db.Column(db.String(50))

    __mapper_args__ = {"polymorphic_identity": "DESIGN"}


class CaseTrademark(Case):
    __tablename__ = "case_trademarks"
    id = db.Column(db.Integer, db.ForeignKey("cases.id"), primary_key=True)

    tm_type = db.Column(db.String(30))  # TrademarkType (General, ,  )
    tm_name = db.Column(db.String(200))  # Trademark

    app_type = db.Column(db.String(30))  # Filing type
    app_route = db.Column(db.String(30))  # Madrid, Paris

    # Dates & Numbers
    pub_date = db.Column(db.Date)
    pub_no = db.Column(db.String(50))
    reg_date = db.Column(db.Date)
    reg_no = db.Column(db.String(50))

    priority_date = db.Column(db.Date)
    priority_no = db.Column(db.String(50))

    original_app_date = db.Column(db.Date)
    original_app_no = db.Column(db.String(50))
    original_reg_date = db.Column(db.Date)
    original_reg_no = db.Column(db.String(50))

    # Goods
    nice_classes = db.Column(db.String(100))  #  (e.g. "09, 35, 42")
    designated_goods = db.Column(db.Text)  #  (Text area)

    # {"nice_class":[9,35,42], "goods":"..."} (Legacy JSON field, keeping for compatibility if needed)
    goods_info = db.Column(db.JSON)

    __mapper_args__ = {"polymorphic_identity": "TRADEMARK"}


class CaseLitigation(Case):
    __tablename__ = "case_litigations"
    id = db.Column(db.Integer, db.ForeignKey("cases.id"), primary_key=True)

    trial_type = db.Column(db.String(50))  # Void, Confirm 
    court = db.Column(db.String(50))  # Patent, Patent, 
    plaintiff = db.Column(db.String(100))  # Billing/
    defendant = db.Column(db.String(100))  # Billing/
    result = db.Column(db.String(20))

    # Litigation specific dates (distinct from Case.filing_date usually, but using Case.filing_date for 'request date')
    judgment_date = db.Column(db.Date)
    winning_party = db.Column(db.String(50))  # 

    related_case_id = db.Column(db.Integer)  #  Matter ID (e.g. invalidating a patent)

    # 1st/2nd/3rd instance info could be JSON or specific fields. Keeping simple for now.

    __mapper_args__ = {"polymorphic_identity": "LITIGATION"}


class CaseForeignInfo(db.Model):
    __tablename__ = "case_foreign_infos"

    case_id = db.Column(db.Integer, db.ForeignKey("cases.id"), primary_key=True)
    # foreign_agent_id could reference partners.id in future
    foreign_agent_id = db.Column(db.Integer)
    translation_due_date = db.Column(db.Date)
    translation_fee = db.Column(db.Numeric(12, 2))
    pct_app_date = db.Column(db.Date)
    pct_app_no = db.Column(db.String(50))
    ep_app_date = db.Column(db.Date)
    ep_app_no = db.Column(db.String(50))
