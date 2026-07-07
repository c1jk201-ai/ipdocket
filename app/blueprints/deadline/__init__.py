from flask import Blueprint

bp = Blueprint("deadlines", __name__)

from app.blueprints.deadline import commands, routes  # noqa: E402,F401
