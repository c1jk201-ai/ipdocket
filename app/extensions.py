from flask import request
from flask_limiter import Limiter
from flask_login import AnonymousUserMixin, LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy(session_options={"expire_on_commit": False})
login_manager = LoginManager()
csrf = CSRFProtect()


def _rate_limit_key() -> str:
    """
    SECURITY: Do not read X-Forwarded-For directly. ProxyFix normalizes
    request.remote_addr only for configured trusted proxy peers.
    """
    try:
        return request.remote_addr or "unknown"
    except Exception:
        return "unknown"


limiter = Limiter(key_func=_rate_limit_key)

# NOTE:
# Configure rate limiting from RATELIMIT_STORAGE_URI.
# memory://   from  .

# Redirect unauthenticated users to the local login page.
login_manager.login_view = "auth.login"
login_manager.login_message_category = "info"


class AnonymousUser(AnonymousUserMixin):
    id = None
    role = ""
    username = "Guest"
    email = ""
    department = ""
    position = ""


login_manager.anonymous_user = AnonymousUser
