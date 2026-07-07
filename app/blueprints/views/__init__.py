from flask import Blueprint

bp = Blueprint("views", __name__)

from app.blueprints.views import routes  # noqa: E402,F401
