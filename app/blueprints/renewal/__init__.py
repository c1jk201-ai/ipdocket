from flask import Blueprint

bp = Blueprint("annuities", __name__)

from app.blueprints.renewal import routes  # noqa: E402,F401
from app.services.annuity.annuity_listeners import register_listeners

register_listeners()
