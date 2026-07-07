from __future__ import annotations

import argparse
import getpass
import os


def _config_name() -> str:
    cfg = (os.environ.get("FLASK_CONFIG") or "").strip().lower()
    if cfg in {"development", "production", "default"}:
        return cfg
    env = (os.environ.get("FLASK_ENV") or os.environ.get("ENV") or "").strip().lower()
    return "production" if env in {"prod", "production"} else "development"


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or update a local admin user.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--email", default="")
    parser.add_argument("--display-name", default="")
    parser.add_argument("--role", default="admin")
    parser.add_argument("--password", default="")
    args = parser.parse_args()

    password = args.password or getpass.getpass("Password: ")
    if not password:
        raise SystemExit("Password is required.")

    os.environ.setdefault("SCHEDULER_ENABLED", "0")
    os.environ.setdefault("RUN_SCHEDULER", "0")
    os.environ.setdefault("DB_SCHEMA_AUTO_CREATE", "0")

    from app import create_app
    from app.extensions import db
    from app.services.local_auth import upsert_local_user

    app = create_app(_config_name(), enable_bootstrap=False, enable_scheduler=False)
    with app.app_context():
        result = upsert_local_user(
            username=args.username,
            password=password,
            email=args.email,
            display_name=args.display_name,
            role_name=args.role,
            is_active=True,
        )
        db.session.commit()
        action = "created" if result.created else "updated"
        print(f"Local user {action}: {args.username} ({args.role})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
