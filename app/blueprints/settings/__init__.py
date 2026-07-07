from flask import Blueprint

bp = Blueprint("settings", __name__)

from app.blueprints.settings import routes  # noqa: E402,F401
