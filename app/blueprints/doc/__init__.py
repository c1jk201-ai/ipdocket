from flask import Blueprint

bp = Blueprint("doc", __name__)

from app.blueprints.doc import routes  # noqa: E402,F401
