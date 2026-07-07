from flask import Blueprint

bp = Blueprint("admin", __name__)

from . import routes  # noqa: E402,F401
from . import matter_menu  # noqa: E402,F401
from . import parameters  # noqa: E402,F401
from . import roles
