from flask import Blueprint

bp = Blueprint("worklog", __name__)

from app.blueprints.worklog import routes  # noqa: E402,F401
