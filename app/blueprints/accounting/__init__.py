from flask import Blueprint

bp = Blueprint("costs", __name__)

from app.blueprints.accounting import legacy_api  # noqa: E402,F401
from app.blueprints.accounting import routes  # noqa: E402,F401
