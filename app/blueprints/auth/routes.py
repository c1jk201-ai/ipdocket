from __future__ import annotations

from flask import abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.blueprints.auth import bp
from app.extensions import limiter
from app.models.user import User
from app.services.core.config_service import ConfigService
from app.utils.permissions import is_admin
from app.utils.url_helpers import safe_next_url


def _get_runtime_bool(key: str, *, default: bool = False) -> bool:
    return ConfigService.get_bool(key, default, prefer_env=True)


def _is_test_account_username(username: str | None) -> bool:
    value = (username or "").strip().lower()
    return bool(value) and value.startswith("test_")


def _login_with_password(*, username: str, password: str) -> User | None:
    user = User.query.filter_by(username=(username or "").strip()).first()
    if user is None or not user.check_password(password) or not user.is_active:
        return None
    return user


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("60 per minute", error_message="Login   .   Retry.")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    allow_password = _get_runtime_bool("ALLOW_PASSWORD_LOGIN", default=True)
    auth_warning = None
    if not allow_password:
        auth_warning = "Password Login disabled exists. Administrator ."

    if request.method == "POST":
        if not allow_password:
            flash(auth_warning, "warning")
            return redirect(url_for("auth.login", next=request.args.get("next")))

        user = _login_with_password(
            username=request.form.get("username") or "",
            password=request.form.get("password") or "",
        )
        if user is None:
            flash("Login Failed.", "warning")
            return redirect(url_for("auth.login", next=request.args.get("next")))

        login_user(user, remember=_get_runtime_bool("AUTH_REMEMBER_ENABLED", default=False))
        next_page = safe_next_url(request.args.get("next")) or url_for("main.index")
        return redirect(next_page)

    return render_template(
        "auth/login.html",
        auth_warning=auth_warning,
        show_password_login=bool(allow_password),
    )


@bp.route("/test-login", methods=["GET", "POST"])
@limiter.limit("60 per minute", error_message="Login   .   Retry.")
def test_login():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    if not _get_runtime_bool("ENABLE_TEST_ACCOUNTS", default=False):
        flash(" Account Login disabled exists.", "warning")
        return redirect(url_for("auth.login", next=request.args.get("next")))

    next_arg = request.args.get("next")
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        if not _is_test_account_username(username):
            flash("Login Failed.", "warning")
            return redirect(url_for("auth.test_login", next=next_arg))

        user = _login_with_password(username=username, password=request.form.get("password") or "")
        if user is None:
            flash("Login Failed.", "warning")
            return redirect(url_for("auth.test_login", next=next_arg))

        login_user(user, remember=_get_runtime_bool("AUTH_REMEMBER_ENABLED", default=False))
        next_page = safe_next_url(next_arg) or url_for("main.index")
        return redirect(next_page)

    return render_template("auth/test_login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    logout_user()
    session.clear()
    resp = redirect(url_for("auth.login"))
    resp.delete_cookie("remember_token")
    resp.delete_cookie("session")
    return resp


@bp.route("/whoami")
@login_required
def whoami():
    if not is_admin(current_user):
        abort(404)
    return jsonify(
        {
            "is_authenticated": True,
            "user_id": getattr(current_user, "id", None),
            "email": getattr(current_user, "email", ""),
            "role": getattr(current_user, "role", ""),
        }
    )
