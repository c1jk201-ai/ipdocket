from flask import Blueprint

bp = Blueprint("workflow", __name__)

from app.blueprints.workflow import routes
