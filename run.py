import os

from app import create_app as create_flask_app
from app.extensions import db

# Ensure IPM models are loaded for table creation
# Legacy IPM models import for DB creation
from app.models import ip_records
from app.models.case import Case
from app.models.user import User
from app.models.workflow import Workflow


def _config_name() -> str:
    cfg = (os.environ.get("FLASK_CONFIG") or "").strip().lower()
    if cfg in {"development", "production", "default"}:
        return cfg
    env = (os.environ.get("FLASK_ENV") or os.environ.get("ENV") or "").strip().lower()
    return "production" if env in {"prod", "production"} else "development"


# Create Flask app
app = create_flask_app(_config_name())


# Shell context for the app
@app.shell_context_processor
def make_shell_context():
    return {"db": db, "User": User, "Case": Case, "Workflow": Workflow}


if __name__ == "__main__":
    # Run the Flask application directly
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=bool(app.debug),
        threaded=True,
    )
