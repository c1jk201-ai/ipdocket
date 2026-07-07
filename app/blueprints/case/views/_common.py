"""Common utilities for upload views.

Shared helper functions for all upload-related views.
"""

from __future__ import annotations

from flask import flash, redirect, render_template, url_for
from flask_wtf.csrf import generate_csrf, validate_csrf


def validate_csrf_or_redirect(
    form_token: str | None,
    redirect_endpoint: str,
    **redirect_kwargs,
):
    """
    Validate CSRF token or return redirect response.

    Args:
        form_token: CSRF token from form submission
        redirect_endpoint: Endpoint to redirect to on failure
        **redirect_kwargs: Additional kwargs for url_for

    Returns:
        None if valid, redirect Response if invalid
    """
    try:
        validate_csrf(form_token)
        return None
    except Exception:
        flash("CSRF  does not. page New  Retry.", "danger")
        return redirect(url_for(redirect_endpoint, **redirect_kwargs))


def render_duplicate_confirm(
    *,
    duplicates: list[dict],
    confirm_url: str,
    cancel_url: str,
    upload_session_id: str,
    extra_hidden: dict | None = None,
):
    """
    Render standard duplicate confirmation page.

    Args:
        duplicates: List of duplicate file info dicts
        confirm_url: URL to POST confirmation to
        cancel_url: URL for cancel button
        upload_session_id: Session ID for staged files
        extra_hidden: Additional hidden fields to include

    Returns:
        Rendered template response
    """
    hidden_fields = {"upload_session_id": upload_session_id}
    if extra_hidden:
        hidden_fields.update(extra_hidden)

    return render_template(
        "case/duplicate_confirm.html",
        duplicates=duplicates,
        confirm_url=confirm_url,
        cancel_url=cancel_url,
        hidden_fields=hidden_fields,
        csrf_token_input=f'<input type="hidden" name="csrf_token" value="{generate_csrf()}">',
    )


def render_popup_done(*, title: str, back_url: str):
    """Render popup completion page."""
    return render_template(
        "case/popup_done.html",
        title=title,
        back_url=back_url,
    )


def parse_popup_param(request) -> str | None:
    """Extract popup parameter from request."""
    return request.args.get("popup")
