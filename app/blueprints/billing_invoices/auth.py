from __future__ import annotations

import json
from functools import wraps
from typing import Any

from flask import abort, current_app, g, has_request_context, redirect, request, url_for
from flask_login import current_user

from .db import get_db


def get_current_user():
  """
  Returns a dict-like representation of the current user for compatibility
  with legacy invoice templates/logic.
  """
  if not getattr(current_user, "is_authenticated", False):
    return None

  # Map Flask-Login User model to dict
  user_id = getattr(current_user, "id", None)
  username = getattr(current_user, "username", None)
  email = getattr(current_user, "email", None)
  if not username:
    username = email or (str(user_id) if user_id is not None else "")

  return {
    "id": user_id,
    "username": username,
    "role": (getattr(current_user, "role", "") or "").strip(),
    "display_name": (
      (getattr(current_user, "display_name", None) or "").strip()
      or (str(username) if username is not None else "")
    ),
    "email": email,
    "is_active": bool(getattr(current_user, "is_active", True)),
  }


def login_required(view):
  @wraps(view)
  def wrapped(*args, **kwargs):
    if not current_user.is_authenticated:
      return redirect(url_for("auth.login", next=request.path))
    return view(*args, **kwargs)

  return wrapped


from app.utils.permissions import get_invoice_roles, get_management_roles, get_user_role_names


def role_required(*roles):
  def decorator(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
      if not current_user.is_authenticated:
        return redirect(url_for("auth.login", next=request.path))

      user_roles = get_user_role_names(current_user)

      # 1. Admin superuser bypass
      if "admin" in user_roles:
        return view(*args, **kwargs)

      # 2. Expand role groups
      # - 'manager' expands to configured management roles
      # - 'staff' expands to configured invoice roles
      extended_roles = {r.lower() for r in roles}
      if "manager" in extended_roles:
        extended_roles.update(get_management_roles())
      if "staff" in extended_roles:
        extended_roles.update(get_invoice_roles())

      if not user_roles.intersection(extended_roles):
        abort(403, "Permission denied.")
      return view(*args, **kwargs)

    return wrapped

  return decorator


def _mask_email(email: str) -> str:
  """Mask an email address for audit logging to minimise PII exposure.

  Example: ``user@example.com`` → ``u***@e***.com``
  """
  if "@" not in email:
    return email
  local, domain = email.rsplit("@", 1)
  masked_local = local[0] + "***" if local else "***"
  parts = domain.rsplit(".", 1)
  if len(parts) == 2:
    masked_domain = parts[0][0] + "***" + "." + parts[1]
  else:
    masked_domain = domain[0] + "***" if domain else "***"
  return f"{masked_local}@{masked_domain}"


def _actor_audit_meta(user: Any, actor_id: Any) -> dict[str, Any]:
  raw_username = str(getattr(user, "username", None) or "").strip()
  raw_email = str(getattr(user, "email", None) or "").strip()

  if raw_username:
    username = raw_username
  elif raw_email:
    username = _mask_email(raw_email)
  else:
    username = str(actor_id)

  display_name = str(getattr(user, "display_name", None) or "").strip() or username
  return {
    "actor_user_id": actor_id,
    "actor_username": username,
    "actor_display_name": display_name,
  }


def _normalize_audit_meta(meta: Any, *, actor_meta: dict[str, Any]) -> str:
  if meta is None or meta == "":
    payload: dict[str, Any] = {}
  elif isinstance(meta, dict):
    payload = dict(meta)
  elif isinstance(meta, str):
    try:
      parsed = json.loads(meta)
    except Exception:
      parsed = None
    if isinstance(parsed, dict):
      payload = parsed
    elif parsed is not None:
      payload = {"value": parsed}
    else:
      payload = {"raw_meta": meta}
  else:
    payload = {"value": str(meta)}

  for key, value in actor_meta.items():
    payload[key] = value
  return json.dumps(payload, ensure_ascii=False)


def log_audit(action: str, target_type: str = None, target_id: int = None, meta: str = None):
  try:
    user = current_user
  except Exception:
    return

  if not getattr(user, "is_authenticated", False):
    return

  request_id = None
  if has_request_context():
    request_id = getattr(g, "request_id", None)
  actor_id = getattr(user, "id", None)
  if actor_id is None:
    return
  actor_meta = _actor_audit_meta(user, actor_id)
  normalized_meta = _normalize_audit_meta(meta, actor_meta=actor_meta)

  conn = get_db()
  try:
    try:
      conn.execute(
        "INSERT INTO audit_log (request_id, actor_id, user_id, action, target_type, target_id, meta) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (request_id, actor_id, actor_id, action, target_type, target_id, normalized_meta),
      )
    except Exception:
      conn.execute(
        "INSERT INTO audit_log (user_id, action, target_type, target_id, meta) VALUES (?, ?, ?, ?, ?)",
        (actor_id, action, target_type, target_id, normalized_meta),
      )
    conn.commit()
  except Exception:
    current_app.logger.exception("Failed to write audit log")
  finally:
    conn.close()

  # Invoice timeline and manager follow-up are processed after the DB
  # connection is safely closed so that failures in these downstream calls
  # never leave the connection hanging.
  if target_type == "invoice" and target_id:
    try:
      from app.services.billing.invoice_timeline_service import record_invoice_timeline_event

      record_invoice_timeline_event(
        action=action,
        invoice_id=int(target_id),
        meta=normalized_meta,
        request_id=request_id,
        actor_id=actor_id,
        actor_name=actor_meta.get("actor_display_name") or actor_meta.get("actor_username"),
      )
    except Exception:
      current_app.logger.exception("Failed to record invoice timeline event")
    try:
      from app.services.billing.invoice_manager_followup_service import (
        maybe_notify_manager_followup_for_invoice,
      )

      maybe_notify_manager_followup_for_invoice(
        action=action,
        invoice_id=int(target_id),
        meta=normalized_meta,
        actor_id=actor_id,
      )
    except Exception:
      current_app.logger.exception("Failed to notify invoice manager follow-up")
