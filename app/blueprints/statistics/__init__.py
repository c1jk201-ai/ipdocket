from flask import Blueprint

bp = Blueprint("stats", __name__)

from app.blueprints.statistics import routes  # noqa: E402,F401
