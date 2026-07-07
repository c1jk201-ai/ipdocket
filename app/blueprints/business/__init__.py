from flask import Blueprint

bp = Blueprint("business", __name__)

from app.blueprints.business import routes  # noqa: E402,F401
