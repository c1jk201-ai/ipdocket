from __future__ import annotations

import logging
from urllib.parse import urlencode

from sqlalchemy import func, or_

from app.extensions import db
from app.models.client import Client
from app.models.matter import Matter, MatterCustomField, MatterPartyRole

logger = logging.getLogger(__name__)


def _clean_text(value: object | None) -> str:
  return str(value or "").strip()


def resolve_matter_client_id(*, matter: Matter | None = None, matter_id: str | None = None) -> str:
  mid = _clean_text(matter_id or getattr(matter, "matter_id", None))
  if not mid:
    return ""

  client_name = ""
  try:
    rows = (
      MatterCustomField.query.with_entities(
        MatterCustomField.namespace, MatterCustomField.data
      )
      .filter(MatterCustomField.matter_id == mid)
      .all()
    )
    rows = sorted(rows, key=lambda item: 0 if _clean_text(item[0]) == "basic" else 1)
    for _namespace, data in rows:
      if not isinstance(data, dict):
        continue
      resolved_client_id = _clean_text(data.get("client_id"))
      if resolved_client_id:
        return resolved_client_id
      if not client_name:
        client_name = _clean_text(data.get("client_name"))
  except Exception:
    rows = []

  try:
    party_rows = (
      db.session.query(MatterPartyRole.party_id)
      .filter(MatterPartyRole.matter_id == mid)
      .filter(func.lower(func.coalesce(MatterPartyRole.role_code, "")) == "client")
      .filter(func.coalesce(MatterPartyRole.party_id, "") != "")
      .order_by(func.coalesce(MatterPartyRole.seq, 0).asc())
      .all()
    )
    party_ids: list[str] = []
    for row in party_rows or []:
      pid = _clean_text(
        getattr(row, "party_id", None) if hasattr(row, "party_id") else row[0]
      )
      if pid and pid not in party_ids:
        party_ids.append(pid)
    if party_ids:
      clients = (
        Client.query.filter(or_(Client.is_deleted.is_(False), Client.is_deleted.is_(None)))
        .filter(or_(Client.party_id.in_(party_ids), Client.ipm_party_id.in_(party_ids)))
        .all()
      )
      by_party: dict[str, Client] = {}
      for client in clients:
        for pid in (
          _clean_text(getattr(client, "party_id", None)),
          _clean_text(getattr(client, "ipm_party_id", None)),
        ):
          if pid:
            by_party[pid] = client

      resolved_clients: list[Client] = []
      for pid in party_ids:
        client = by_party.get(pid)
        if client and client not in resolved_clients:
          resolved_clients.append(client)
      if len(resolved_clients) == 1:
        return _clean_text(getattr(resolved_clients[0], "id", None))
  except Exception:
    logger.warning("Failed to resolve matter client_id from MatterPartyRole", exc_info=True)

  if not client_name:
    return ""

  try:
    matches = (
      Client.query.filter(or_(Client.is_deleted.is_(False), Client.is_deleted.is_(None)))
      .filter(Client.name == client_name)
      .limit(2)
      .all()
    )
  except Exception:
    matches = []
  if len(matches) == 1:
    return _clean_text(getattr(matches[0], "id", None))
  return ""


def build_invoice_create_params(
  *,
  matter: Matter | None = None,
  matter_id: str | None = None,
  our_ref: str | None = None,
  worklog_ids: str | None = None,
) -> dict[str, str]:
  mid = _clean_text(matter_id or getattr(matter, "matter_id", None))
  ref = _clean_text(our_ref or getattr(matter, "our_ref", None))

  params: dict[str, str] = {}
  if mid:
    params["ipm_case_id"] = mid
  if ref:
    params["ipm_case_ref"] = ref

  client_id = resolve_matter_client_id(matter=matter, matter_id=mid)
  if client_id:
    params["client_id"] = client_id

  worklog_ids_value = _clean_text(worklog_ids)
  if worklog_ids_value:
    params["worklog_ids"] = worklog_ids_value

  return params


def resolve_invoice_create_base_url(*, config=None) -> str:
  cfg = config
  for key in ("INVOICE_MODULE_CREATE_URL", "INVOICE_CREATE_URL"):
    try:
      base = _clean_text(cfg.get(key)) if cfg is not None else ""
    except Exception:
      base = ""
    if base:
      return base

  try:
    view_base = _clean_text(cfg.get("INVOICE_MODULE_VIEW_BASE_URL")) if cfg is not None else ""
  except Exception:
    view_base = ""
  if view_base:
    root = view_base.rstrip("/")
    if root.endswith("/invoices"):
      root = root[: -len("/invoices")]
    if root:
      return f"{root}/invoices/create"

  return "/accounting/invoice-system/invoices/create"


def build_invoice_create_url(
  base_url: str,
  *,
  matter: Matter | None = None,
  matter_id: str | None = None,
  our_ref: str | None = None,
  worklog_ids: str | None = None,
) -> str:
  base = _clean_text(base_url)
  if not base:
    return ""

  params = build_invoice_create_params(
    matter=matter,
    matter_id=matter_id,
    our_ref=our_ref,
    worklog_ids=worklog_ids,
  )
  if not params:
    return base

  joiner = "&" if "?" in base else "?"
  return f"{base}{joiner}{urlencode(params)}"
