from flask import Blueprint

bp = Blueprint("customers", __name__)

from app.blueprints.crm import routes  # noqa: E402,F401
