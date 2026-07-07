from __future__ import annotations

import os
from dataclasses import dataclass

from flask import Flask

from app.extensions import db
from app.models.role import Role
from app.models.user import User
from app.security.default_role_profiles import get_default_role_profiles

BUILTIN_ROLES = (
    "user",
    "admin",
    "mgmt_director",
    "mgmt_staff",
    "lead_attorney",
    "partner_attorney",
    "patent_staff",
)


@dataclass(frozen=True)
class LocalUserResult:
    user: User
    created: bool
    password_updated: bool


def ensure_builtin_roles() -> None:
    """Create baseline roles and refresh their default permissions."""
    profiles = get_default_role_profiles()
    for role_name in BUILTIN_ROLES:
        role = Role.query.filter_by(name=role_name).first()
        if role is None:
            role = Role(name=role_name, description=f"Built-in role: {role_name}")
            db.session.add(role)
        if role_name in profiles:
            role.permissions = list(dict.fromkeys(profiles[role_name]))


def upsert_local_user(
    *,
    username: str,
    password: str | None = None,
    email: str | None = None,
    display_name: str | None = None,
    role_name: str = "admin",
    is_active: bool = True,
) -> LocalUserResult:
    username = (username or "").strip()
    if not username:
        raise ValueError("username is required")

    role_name = (role_name or "admin").strip().lower()
    ensure_builtin_roles()
    role = Role.query.filter_by(name=role_name).first()
    if role is None:
        role = Role(name=role_name, description=f"Local role: {role_name}", permissions=[])
        db.session.add(role)
        db.session.flush()

    user = User.query.filter_by(username=username).first()
    created = user is None
    if user is None:
        user = User(username=username)
        db.session.add(user)

    if email is not None:
        user.email = (email or "").strip() or None
    if display_name is not None:
        user.display_name = (display_name or "").strip() or None
    user.role = role_name
    user.is_active = bool(is_active)

    password_updated = False
    if password:
        user.set_password(password)
        password_updated = True

    if role not in (user.roles or []):
        user.roles.append(role)

    return LocalUserResult(user=user, created=created, password_updated=password_updated)


def bootstrap_local_admin_from_env(app: Flask) -> None:
    """Create or update the first local admin when explicit env credentials are set."""
    enabled = str(
        app.config.get(
            "LOCAL_ADMIN_BOOTSTRAP_ENABLED",
            os.environ.get("LOCAL_ADMIN_BOOTSTRAP_ENABLED", "0"),
        )
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return

    username = (os.environ.get("LOCAL_ADMIN_USERNAME") or "").strip()
    password = os.environ.get("LOCAL_ADMIN_PASSWORD") or ""
    if not username or not password:
        return

    with app.app_context():
        result = upsert_local_user(
            username=username,
            password=password,
            email=os.environ.get("LOCAL_ADMIN_EMAIL"),
            display_name=os.environ.get("LOCAL_ADMIN_DISPLAY_NAME"),
            role_name=os.environ.get("LOCAL_ADMIN_ROLE") or "admin",
            is_active=True,
        )
        db.session.commit()
        app.logger.info(
            "Local admin bootstrap %s for username=%s",
            "created" if result.created else "updated",
            username,
        )
