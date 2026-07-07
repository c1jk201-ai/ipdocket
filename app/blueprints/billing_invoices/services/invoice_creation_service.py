from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from zoneinfo import ZoneInfo

from flask import abort, current_app, flash, redirect, render_template, request, url_for

from app.services.billing.invoice_prefill import resolve_matter_client_id
from app.services.billing.utils import compute_totals, compute_totals_minor, d, to_minor
from app.services.client.client_tagging import build_client_search_tags_text
from app.utils.error_logging import report_swallowed_exception

from ..auth import get_current_user, log_audit
from ..db import (
  _execute_insert_returning_id,
  _get_column_names,
  build_client_deposit_audit_meta,
  get_all_business_profiles,
  get_business_profile,
  get_client_deposit_balance_minor,
  insert_client_deposit_ledger_entry,
  next_invoice_number,
  row_get,
  safe_json_parse,
  snapshot_of_profile,
  unified_clients_enabled,
)


@dataclass(frozen=True)
class InvoiceCreateHooks:
  ensure_external_invoice_case_map: Callable[..., None]
  safe_int: Callable[..., int | None]
  parse_settlement_splits: Callable[..., tuple[list[dict], str | None, str | None]]
  normalize_invoice_date: Callable[[Any], str | None]
  parse_amount_to_minor: Callable[[str, str], int]
  compute_billing_payment_from_status: Callable[[str, int], tuple[str, str]]
  derive_legacy_status_from_split: Callable[[str, str], str]
  resolve_invoice_case_target: Callable[..., dict]
  sync_legacy_status: Callable[..., str]


@dataclass
class InvoiceCreatePageState:
  conn: Any
  clients: list[Any]
  all_profiles: list[dict]
  templates: list[Any]
  default_business_profile: dict | None
  ipm_case_id_q: str
  ipm_case_ref_q: str
  prefill_client_id: str
  outgoing_mode: bool


_PREFILL_MAX_ITEMS = 100
_PREFILL_TRUE_TOKENS = {
  "1",
  "true",
  "t",
  "yes",
  "y",
  "on",
  "estimated",
  "estimate",
  "expected",
}
_PREFILL_FALSE_TOKENS = {
  "0",
  "false",
  "f",
  "no",
  "n",
  "off",
  "actual",
  "charge",
}
_PREFILL_HEADER_TOKENS = {
  "name",
  "item",
  "description",
  "type",
  "quantity",
  "qty",
  "unit price",
  "discount",
  "discount(%)",
  "amount",
  "estimate",
  "estimated",
}
_PREFILL_ITEM_TYPE_ALIASES = {
  "service": "service",
  "service_fee": "service",
  "service fee": "service",
  "fee": "service",
  "admin": "admin",
  "official": "admin",
  "official_fee": "admin",
  "official fee": "admin",
  "gov": "admin",
  "government": "admin",
  "government_fee": "admin",
  "government fee": "admin",
  "uspto": "admin",
  "patent office": "admin",
  "foreign": "foreign",
  "foreign_fee": "foreign",
  "foreign fee": "foreign",
  "foreign_cost": "foreign",
  "foreign cost": "foreign",
}


def _default_dates() -> tuple[str, str]:
  today = date.today().isoformat()
  next_month = (date.today() + timedelta(days=30)).isoformat()
  return today, next_month


def _clean_prefill_text(value: Any) -> str:
  return str(value or "").strip()


def _first_prefill_value(row: dict[str, Any], keys: tuple[str, ...]) -> str:
  for key in keys:
    value = row.get(key)
    if isinstance(value, (list, tuple)):
      value = value[0] if value else ""
    text = _clean_prefill_text(value)
    if text:
      return text
  return ""


def _prefill_bool_or_none(value: Any) -> int | None:
  text = _clean_prefill_text(value)
  if not text:
    return None
  key = text.lower()
  if key in _PREFILL_TRUE_TOKENS:
    return 1
  if key in _PREFILL_FALSE_TOKENS:
    return 0
  return None


def _normalize_prefill_item_type(value: Any, *, default: str | None = "service") -> str | None:
  text = _clean_prefill_text(value)
  if not text:
    return default
  key = text.lower().replace("-", "_").strip()
  normalized = _PREFILL_ITEM_TYPE_ALIASES.get(key) or _PREFILL_ITEM_TYPE_ALIASES.get(text)
  if normalized:
    return normalized
  return default


def _normalize_prefill_number(value: Any, *, default: str) -> str:
  text = _clean_prefill_text(value)
  if not text:
    return default
  normalized = text.replace(",", "")
  normalized = re.sub(r"(?i)\b(?:usd|usd|eur|jpy|cny)\b", "", normalized)
  normalized = normalized.replace("$", "").replace("$", "").strip()
  try:
    return str(Decimal(normalized))
  except (InvalidOperation, ValueError):
    return default


def _normalize_prefill_discount(value: Any) -> str:
  text = _normalize_prefill_number(value, default="0")
  try:
    pct = Decimal(text)
  except (InvalidOperation, ValueError):
    pct = Decimal("0")
  pct = max(Decimal("0"), min(Decimal("100"), pct))
  return str(pct)


def _normalize_prefill_phase(value: Any) -> str:
  text = _clean_prefill_text(value).lower()
  if text in {"oa", "office_action", "office action", "examination", "office-action"}:
    return "oa"
  if text in {"reg", "registration"}:
    return "reg"
  return "app"


def _build_prefill_item(row: dict[str, Any]) -> dict[str, Any] | None:
  description = _first_prefill_value(
    row,
    (
      "description",
      "desc",
      "name",
      "title",
      "item_name",
    ),
  )
  if not description:
    return None

  item_type = _normalize_prefill_item_type(
    _first_prefill_value(row, ("item_type", "type", "category"))
  )
  if item_type not in {"service", "admin", "foreign"}:
    item_type = "service"

  estimated = _prefill_bool_or_none(
    _first_prefill_value(
      row,
      (
        "is_estimated",
        "estimated",
        "is_estimated_base",
        "estimate",
      ),
    )
  )
  if estimated is None:
    estimated = 0

  return {
    "description": description,
    "qty": _normalize_prefill_number(
      _first_prefill_value(row, ("qty", "quantity")),
      default="1",
    ),
    "unit_price": _normalize_prefill_number(
      _first_prefill_value(row, ("unit_price", "price", "unit", "rate")),
      default="0",
    ),
    "item_type": item_type,
    "discount": _normalize_prefill_discount(
      _first_prefill_value(row, ("discount", "discount_pct"))
    ),
    "phase": _normalize_prefill_phase(_first_prefill_value(row, ("phase",))),
    "is_estimated": estimated,
    "is_taxable": 1 if item_type == "service" else 0,
  }


def _prefill_item_from_sequence(values: list[Any]) -> dict[str, Any] | None:
  cells = [_clean_prefill_text(value) for value in values]
  while cells and not cells[-1]:
    cells.pop()
  if len(cells) < 4:
    return None

  headerish = {cell.lower() for cell in cells if cell}
  if headerish and headerish.issubset({token.lower() for token in _PREFILL_HEADER_TOKENS}):
    return None

  row: dict[str, Any]
  second_is_estimated = _prefill_bool_or_none(cells[1]) if len(cells) > 1 else None
  third_type = _normalize_prefill_item_type(cells[2], default=None) if len(cells) > 2 else None
  if second_is_estimated is not None and third_type:
    row = {
      "description": cells[0],
      "is_estimated": second_is_estimated,
      "item_type": cells[2],
      "qty": cells[3] if len(cells) > 3 else "1",
      "unit_price": cells[4] if len(cells) > 4 else "0",
      "discount": cells[5] if len(cells) > 5 else "0",
    }
  else:
    row = {
      "description": cells[0],
      "item_type": cells[1] if len(cells) > 1 else "service",
      "qty": cells[2] if len(cells) > 2 else "1",
      "unit_price": cells[3] if len(cells) > 3 else "0",
      "discount": cells[4] if len(cells) > 4 else "0",
      "is_estimated": cells[5] if len(cells) > 5 else "0",
    }

  return _build_prefill_item(row)


def _parse_prefill_delimited_row(raw: str) -> dict[str, Any] | None:
  text = _clean_prefill_text(raw)
  if not text:
    return None
  delimiter = "\t" if "\t" in text else ("|" if "|" in text else None)
  if not delimiter:
    return None
  return _prefill_item_from_sequence(text.split(delimiter))


def _looks_like_prefill_amount(value: Any) -> bool:
  text = _clean_prefill_text(value)
  if not text:
    return False
  return _normalize_prefill_number(text, default="") != ""


def _parse_prefill_vertical_lines(lines: list[str]) -> list[dict[str, Any]]:
  cells = [_clean_prefill_text(line) for line in lines if _clean_prefill_text(line)]
  while cells and cells[0].lower() in {token.lower() for token in _PREFILL_HEADER_TOKENS}:
    cells.pop(0)

  items: list[dict[str, Any]] = []
  idx = 0
  while idx < len(cells) and len(items) < _PREFILL_MAX_ITEMS:
    description = cells[idx]
    idx += 1
    if description.lower() in {token.lower() for token in _PREFILL_HEADER_TOKENS}:
      continue

    estimated = 0
    if idx < len(cells):
      marker = _prefill_bool_or_none(cells[idx])
      if marker is not None:
        estimated = marker
        idx += 1

    if idx >= len(cells):
      break
    item_type_raw = cells[idx]
    item_type = _normalize_prefill_item_type(item_type_raw, default=None)
    if item_type is None:
      continue
    idx += 1

    if idx + 2 >= len(cells):
      break
    item = _build_prefill_item(
      {
        "description": description,
        "item_type": item_type_raw,
        "qty": cells[idx],
        "unit_price": cells[idx + 1],
        "discount": cells[idx + 2],
        "is_estimated": estimated,
      }
    )
    idx += 3
    if item:
      items.append(item)

    # Spreadsheet copies often include the calculated amount after discount.
    if idx < len(cells) and _looks_like_prefill_amount(cells[idx]):
      idx += 1

  return items


def _parse_prefill_items_text(raw: str) -> list[dict[str, Any]]:
  text = _clean_prefill_text(raw)
  if not text:
    return []
  rows = [line for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
  if any("\t" in row or "|" in row for row in rows):
    items: list[dict[str, Any]] = []
    for row in rows:
      item = _parse_prefill_delimited_row(row)
      if item:
        items.append(item)
        if len(items) >= _PREFILL_MAX_ITEMS:
          break
    return items
  return _parse_prefill_vertical_lines(rows)


def _get_prefill_values(args, keys: tuple[str, ...]) -> list[str]:
  values: list[str] = []
  for key in keys:
    try:
      values.extend(args.getlist(key))
    except Exception:
      continue
  return values


def _prefill_value_at(values: list[str], index: int, default: str = "") -> str:
  if index < len(values):
    return values[index]
  return default


def _parse_prefill_parallel_args(args) -> list[dict[str, Any]]:
  descriptions = _get_prefill_values(
    args,
    (
      "description[]",
      "description",
      "desc[]",
      "desc",
      "name[]",
      "name",
      "item_name[]",
      "item_name",
    ),
  )
  if not descriptions:
    return []

  item_types = _get_prefill_values(
    args,
    ("item_type[]", "item_type", "type[]", "type", "category[]", "category"),
  )
  qtys = _get_prefill_values(args, ("qty[]", "qty", "quantity[]", "quantity"))
  prices = _get_prefill_values(
    args,
    (
      "unit_price[]",
      "unit_price",
      "price[]",
      "price",
      "unit[]",
      "unit",
      "rate[]",
      "rate",
    ),
  )
  discounts = _get_prefill_values(
    args, ("discount[]", "discount", "discount_pct[]", "discount_pct")
  )
  estimateds = _get_prefill_values(
    args,
    (
      "is_estimated_base[]",
      "is_estimated_base",
      "is_estimated[]",
      "is_estimated",
      "estimated[]",
      "estimated",
    ),
  )
  phases = _get_prefill_values(args, ("phase[]", "phase"))

  items: list[dict[str, Any]] = []
  for idx, description in enumerate(descriptions[:_PREFILL_MAX_ITEMS]):
    item = _build_prefill_item(
      {
        "description": description,
        "item_type": _prefill_value_at(item_types, idx, "service"),
        "qty": _prefill_value_at(qtys, idx, "1"),
        "unit_price": _prefill_value_at(prices, idx, "0"),
        "discount": _prefill_value_at(discounts, idx, "0"),
        "is_estimated": _prefill_value_at(estimateds, idx, "0"),
        "phase": _prefill_value_at(phases, idx, "app"),
      }
    )
    if item:
      items.append(item)
  return items


def _parse_prefill_json_value(raw: str) -> list[dict[str, Any]]:
  text = _clean_prefill_text(raw)
  if not text:
    return []
  try:
    parsed = json.loads(text)
  except (TypeError, ValueError):
    return []
  if isinstance(parsed, dict):
    parsed = parsed.get("items") or parsed.get("line_items") or parsed.get("invoice_items") or []
  if not isinstance(parsed, list):
    return []

  items: list[dict[str, Any]] = []
  for entry in parsed[:_PREFILL_MAX_ITEMS]:
    item = None
    if isinstance(entry, dict):
      item = _build_prefill_item(entry)
    elif isinstance(entry, (list, tuple)):
      item = _prefill_item_from_sequence(list(entry))
    if item:
      items.append(item)
  return items


def parse_invoice_prefill_items(args) -> list[dict[str, Any]]:
  items: list[dict[str, Any]] = []

  for key in ("items", "line_items", "invoice_items"):
    for raw in _get_prefill_values(args, (key,)):
      items.extend(_parse_prefill_json_value(raw))
      if len(items) >= _PREFILL_MAX_ITEMS:
        return items[:_PREFILL_MAX_ITEMS]

  for raw in _get_prefill_values(args, ("item", "line_item", "invoice_item")):
    parsed = _parse_prefill_json_value(raw)
    if not parsed:
      delimited = _parse_prefill_delimited_row(raw)
      parsed = [delimited] if delimited else []
    items.extend(item for item in parsed if item)
    if len(items) >= _PREFILL_MAX_ITEMS:
      return items[:_PREFILL_MAX_ITEMS]

  for key in ("items_text", "items_tsv", "line_items_text"):
    for raw in _get_prefill_values(args, (key,)):
      items.extend(_parse_prefill_items_text(raw))
      if len(items) >= _PREFILL_MAX_ITEMS:
        return items[:_PREFILL_MAX_ITEMS]

  items.extend(_parse_prefill_parallel_args(args))
  return items[:_PREFILL_MAX_ITEMS]


def _build_submitted_invoice(
  form,
  *,
  business_profile_id: int,
  client_id: int | None = None,
  issue_date: str | None = None,
  due_date: str | None = None,
  status_code: str | None = None,
) -> dict[str, Any]:
  today, next_month = _default_dates()
  payload: dict[str, Any] = {
    "business_profile_id": business_profile_id,
    "client_id": client_id,
    "number": form.get("number", ""),
    "internal_reference": form.get("internal_reference", ""),
    "issue_date": issue_date if issue_date is not None else form.get("issue_date", today),
    "due_date": due_date if due_date is not None else form.get("due_date", next_month),
    "status": status_code if status_code is not None else form.get("status", "draft"),
    "notes": form.get("notes", ""),
    "language": form.get("invoice_language") or "en",
    "new_client_name": form.get("new_client_name", ""),
    "new_client_name_en": form.get("new_client_name_en", ""),
    "new_client_phone": form.get("new_client_phone", ""),
    "new_client_email": form.get("new_client_email", ""),
    "new_client_address": form.get("new_client_address", ""),
    "new_client_manager": form.get("new_client_manager", ""),
    "new_client_notes": form.get("new_client_notes", ""),
  }
  return payload


def _build_invoice_form_toggle_url(*, outgoing_mode: bool, invoice_id: int | None = None) -> str:
  params = request.args.to_dict(flat=True)
  params["outgoing"] = "0" if outgoing_mode else "1"
  if invoice_id is not None:
    params["invoice_id"] = str(invoice_id)
  elif not str(params.get("invoice_id") or "").strip():
    params.pop("invoice_id", None)
  return url_for(request.endpoint, **params)


def _coerce_outgoing_mode(value, *, default: bool = False) -> bool:
  if value is None:
    return bool(default)
  return str(value).strip().lower() in {"1", "true", "on", "yes", "y"}


def _stored_outgoing_mode(invoice, *, default: bool = False) -> bool:
  try:
    raw = row_get(invoice, "is_outgoing", default=None)
  except Exception:
    raw = None
  if raw is None:
    return bool(default)
  return _coerce_outgoing_mode(raw, default=default)


def _submitted_outgoing_mode(form, *, default: bool) -> bool:
  return _coerce_outgoing_mode(form.get("is_outgoing"), default=default)


def render_invoice_create_form(
  state: InvoiceCreatePageState,
  *,
  invoice: dict[str, Any] | None = None,
  items: list[dict] | None = None,
  business_profile_id: int | None = None,
  settlement_splits: list[dict] | None = None,
  duplicate_warning: bool = False,
  duplicate_prev_number: str | None = None,
):
  today, next_month = _default_dates()
  bp_row = state.default_business_profile
  if business_profile_id is not None:
    bp_row = get_business_profile(business_profile_id)
  if items is None and request.method == "GET":
    items = parse_invoice_prefill_items(request.args)
  return render_template(
    "invoice_form.html",
    invoice=invoice,
    clients=state.clients,
    prefill_client_id=state.prefill_client_id,
    items=items,
    bp=bp_row,
    business_profile=bp_row,
    all_profiles=state.all_profiles,
    templates=state.templates,
    today=today,
    next_month=next_month,
    outgoing_mode=state.outgoing_mode,
    toggle_mode_url=_build_invoice_form_toggle_url(outgoing_mode=state.outgoing_mode),
    settlement_splits=settlement_splits,
    duplicate_warning=duplicate_warning,
    duplicate_prev_number=duplicate_prev_number,
  )


def load_invoice_create_page_state(
  conn,
  *,
  ensure_external_invoice_case_map: Callable[..., None],
) -> InvoiceCreatePageState:
  ensure_external_invoice_case_map(conn)
  ipm_case_id_q = (request.args.get("ipm_case_id") or "").strip()
  prefill_client_id = (request.args.get("client_id") or "").strip()
  if not prefill_client_id and ipm_case_id_q:
    try:
      prefill_client_id = resolve_matter_client_id(matter_id=ipm_case_id_q)
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoice_create.prefill_client_id",
        log_key="billing_invoices.invoice_create.prefill_client_id",
        log_window_seconds=300,
      )
  clients = conn.execute(
    "SELECT id, name, address, phone, manager "
    "FROM clients WHERE is_deleted IS NOT TRUE ORDER BY name"
  ).fetchall()
  all_profiles = get_all_business_profiles()
  templates = conn.execute("SELECT * FROM invoice_templates ORDER BY name").fetchall()
  return InvoiceCreatePageState(
    conn=conn,
    clients=clients,
    all_profiles=all_profiles,
    templates=templates,
    default_business_profile=get_business_profile(),
    ipm_case_id_q=ipm_case_id_q,
    ipm_case_ref_q=(request.args.get("ipm_case_ref") or "").strip(),
    prefill_client_id=prefill_client_id,
    outgoing_mode=request.args.get("outgoing", "0") == "1",
  )


def _collect_submitted_items(form) -> tuple[list[dict], list[str]]:
  descriptions = form.getlist("description[]")
  qtys = form.getlist("qty[]")
  prices = form.getlist("unit_price[]")
  item_types = form.getlist("item_type[]")
  discounts = form.getlist("discount[]")
  phases = form.getlist("phase[]")
  fx_curs = form.getlist("fx_currency[]")
  fx_fees = form.getlist("fx_fee[]")
  fx_govs = form.getlist("fx_gov[]")
  fx_markups = form.getlist("fx_markup[]")
  fx_rates_used = form.getlist("fx_rate_used[]")
  is_estimateds = form.getlist("is_estimated_base[]")
  foreign_vat_bases = form.getlist("foreign_vat_base[]")

  submitted_items = []
  for i, (desc, qv, pv) in enumerate(zip(descriptions, qtys, prices)):
    if (desc or "").strip() or qv or pv:
      item_type = item_types[i] if i < len(item_types) else "service"
      discount_val = d(discounts[i]) if i < len(discounts) else Decimal("0")
      phase_val = phases[i].strip() if i < len(phases) and phases[i] else "app"
      fx_currency = fx_curs[i].upper().strip() if i < len(fx_curs) and fx_curs[i] else None
      fx_fee = fx_fees[i] if i < len(fx_fees) and fx_fees[i] else "0"
      fx_gov = fx_govs[i] if i < len(fx_govs) and fx_govs[i] else "0"
      fx_markup = fx_markups[i] if i < len(fx_markups) and fx_markups[i] else "3"
      fx_rate_used = fx_rates_used[i] if i < len(fx_rates_used) and fx_rates_used[i] else None
      is_estimated = 1 if (i < len(is_estimateds) and is_estimateds[i] == "1") else 0
      fx_store = item_type == "foreign"

      fv = 0
      try:
        if item_type == "foreign" and i < len(foreign_vat_bases):
          fv = 1 if (foreign_vat_bases[i] == "1") else 0
      except Exception:
        fv = 0

      submitted_items.append(
        {
          "description": (desc or "").strip(),
          "qty": qv,
          "unit_price": pv,
          "item_type": item_type,
          "discount": str(discount_val),
          "phase": phase_val,
          "fx_currency": (fx_currency if fx_store else None),
          "fx_fee": (fx_fee if fx_store else None),
          "fx_gov": (fx_gov if fx_store else None),
          "fx_markup": (fx_markup if fx_store else None),
          "fx_rate_used": (fx_rate_used if fx_store else None),
          "is_estimated": is_estimated,
          "is_taxable": (
            1
            if item_type == "service"
            else (1 if (item_type == "foreign" and fv == 1) else 0)
          ),
        }
      )
  return submitted_items, foreign_vat_bases


def _allowed_settlement_bp_ids(
  all_profiles: list[dict], safe_int: Callable[..., int | None]
) -> set[int] | None:
  try:
    return {
      int(profile["id"])
      for profile in all_profiles
      if profile.get("id") is not None and safe_int(profile.get("id"), None) is not None
    }
  except Exception:
    return None


def _render_validation_error(
  state: InvoiceCreatePageState,
  form,
  submitted_items: list[dict],
  settlement_splits: list[dict] | None,
  *,
  business_profile_id: int,
  client_id: int | None = None,
  issue_date: str | None = None,
  due_date: str | None = None,
  status_code: str | None = None,
  duplicate_warning: bool = False,
  duplicate_prev_number: str | None = None,
):
  return render_invoice_create_form(
    state,
    invoice=_build_submitted_invoice(
      form,
      business_profile_id=business_profile_id,
      client_id=client_id,
      issue_date=issue_date,
      due_date=due_date,
      status_code=status_code,
    ),
    items=submitted_items,
    business_profile_id=business_profile_id,
    settlement_splits=settlement_splits,
    duplicate_warning=duplicate_warning,
    duplicate_prev_number=duplicate_prev_number,
  )


def _digits_only(value: str | None) -> str:
  return "".join(ch for ch in (value or "") if ch.isdigit())


def _clean_client_name_en(value: str | None) -> str:
  return " ".join((value or "").strip().split())


def _merge_client_extra(existing_extra, *, name_en: str | None, overwrite: bool) -> dict[str, Any]:
  merged = dict(safe_json_parse(existing_extra, {}) or {})
  clean_name_en = _clean_client_name_en(name_en)
  if clean_name_en and (overwrite or not str(merged.get("name_en") or "").strip()):
    merged["name_en"] = clean_name_en
  return merged


def _build_client_search_tags(
  *,
  name: str,
  name_en: str | None = None,
  email: str | None = None,
  phone: str | None = None,
) -> str | None:
  values = [
    (name or "").strip(),
    _clean_client_name_en(name_en),
    (email or "").strip(),
    (phone or "").strip(),
  ]
  try:
    tags = build_client_search_tags_text(values, use_llm=False)
  except Exception:
    return None
  return tags or None


def _update_existing_client_fields(
  conn,
  client_id: int,
  *,
  name: str,
  email: str,
  phone: str,
  address: str,
  manager: str,
  notes: str,
  name_en: str,
) -> None:
  try:
    cols = _get_column_names(conn, "clients")
  except Exception:
    cols = set()

  cur_now = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
  if not cur_now:
    return

  updates: dict[str, Any] = {}
  core_updates = {
    "email": email or None,
    "phone": phone or None,
    "address": address or None,
    "manager": manager or None,
    "notes": notes or None,
  }
  for column, incoming in core_updates.items():
    if column not in cols:
      continue
    existing = row_get(cur_now, column, default=None)
    if incoming and not existing:
      updates[column] = incoming

  merged_extra: dict[str, Any] | None = None
  if "extra" in cols:
    existing_extra = row_get(cur_now, "extra", default=None)
    merged_extra = _merge_client_extra(existing_extra, name_en=name_en, overwrite=False)
    if merged_extra != dict(safe_json_parse(existing_extra, {}) or {}):
      updates["extra"] = json.dumps(merged_extra, ensure_ascii=False)

  if "search_tags" in cols:
    effective_email = str(
      updates.get("email") or row_get(cur_now, "email", default="") or ""
    ).strip()
    effective_phone = str(
      updates.get("phone") or row_get(cur_now, "phone", default="") or ""
    ).strip()
    effective_name_en = ""
    if merged_extra is not None:
      effective_name_en = str(merged_extra.get("name_en") or "").strip()
    else:
      effective_name_en = str(
        _merge_client_extra(
          row_get(cur_now, "extra", default=None),
          name_en=name_en,
          overwrite=False,
        ).get("name_en")
        or ""
      ).strip()
    search_tags = _build_client_search_tags(
      name=name or (row_get(cur_now, "name", default="") or ""),
      name_en=effective_name_en,
      email=effective_email,
      phone=effective_phone,
    )
    if search_tags and search_tags != row_get(cur_now, "search_tags", default=None):
      updates["search_tags"] = search_tags

  if not updates:
    return

  sql = "UPDATE clients SET " + ",".join(f"{column}=?" for column in updates) + " WHERE id=?"
  conn.execute(sql, tuple(updates.values()) + (client_id,))


def _insert_client_record(
  conn,
  *,
  name: str,
  email: str,
  phone: str,
  address: str,
  manager: str,
  notes: str,
  name_en: str,
) -> int:
  try:
    cols = _get_column_names(conn, "clients")
  except Exception:
    cols = set()

  insert_columns = ["name", "email", "phone", "address", "manager", "notes"]
  insert_values: list[Any] = [name, email, phone, address, manager, notes]

  if "extra" in cols:
    extra_payload = _merge_client_extra(None, name_en=name_en, overwrite=True)
    insert_columns.append("extra")
    insert_values.append(
      json.dumps(extra_payload, ensure_ascii=False) if extra_payload else None
    )

  if "search_tags" in cols:
    insert_columns.append("search_tags")
    insert_values.append(
      _build_client_search_tags(name=name, name_en=name_en, email=email, phone=phone)
    )

  placeholders = ",".join("?" for _ in insert_columns)
  sql = f"INSERT INTO clients ({','.join(insert_columns)}) VALUES ({placeholders})"
  return _execute_insert_returning_id(conn, sql, tuple(insert_values))


def _resolve_or_create_client(
  conn,
  form,
  *,
  client_id_str: str | None,
  business_profile_id: int,
  submitted_items: list[dict],
  settlement_splits: list[dict] | None,
  state: InvoiceCreatePageState,
  safe_int: Callable[..., int | None],
) -> int:
  if client_id_str:
    client_id = safe_int(client_id_str, None)
    if client_id is not None and client_id <= 0:
      client_id = None
    if client_id is None:
      abort(400, "Invalid client ID.")
    return client_id

  new_client_name = form.get("new_client_name", "").strip()
  if not new_client_name:
    flash("Select a client or enter a new client name.", "error")
    raise _InvoiceCreateRenderError(
      _render_validation_error(
        state,
        form,
        submitted_items,
        settlement_splits,
        business_profile_id=business_profile_id,
      )
    )

  email_val = (form.get("new_client_email", "") or "").strip()
  phone_val = (form.get("new_client_phone", "") or "").strip()
  addr_val = (form.get("new_client_address", "") or "").strip()
  mgr_val = (form.get("new_client_manager", "") or "").strip()
  notes_val = (form.get("new_client_notes", "") or "").strip()
  name_en_val = _clean_client_name_en(form.get("new_client_name_en", ""))

  resolved_client_id = None
  try:
    rows = conn.execute(
      "SELECT id, email, phone FROM clients WHERE name=? AND is_deleted IS NOT TRUE",
      (new_client_name,),
    ).fetchall()
    email_lower = email_val.lower() if email_val else None
    phone_digits = _digits_only(phone_val)
    if rows:
      candidates = []
      for row in rows:
        rid = row_get(row, "id", 0, default=None)
        remail = row_get(row, "email", 1) or ""
        rphone = row_get(row, "phone", 2) or ""
        ok_email = email_lower and remail and email_lower == remail.strip().lower()
        ok_phone = (
          phone_digits and _digits_only(rphone) and phone_digits == _digits_only(rphone)
        )
        if ok_email or ok_phone:
          candidates.append(rid)
      if len(candidates) == 1:
        resolved_client_id = candidates[0]
    if resolved_client_id is None and len(rows) == 1 and not email_val and not phone_val:
      resolved_client_id = row_get(rows[0], "id", 0, default=None)
  except Exception:
    resolved_client_id = None

  if resolved_client_id is not None:
    client_id = int(resolved_client_id)
    try:
      _update_existing_client_fields(
        conn,
        client_id,
        name=new_client_name,
        email=email_val,
        phone=phone_val,
        address=addr_val,
        manager=mgr_val,
        notes=notes_val,
        name_en=name_en_val,
      )
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoice_create.resolve_or_create_client.update_existing",
        log_key="billing_invoices.invoice_create.resolve_or_create_client.update_existing",
        log_window_seconds=300,
      )
    return client_id

  return _insert_client_record(
    conn,
    name=new_client_name,
    email=email_val,
    phone=phone_val,
    address=addr_val,
    manager=mgr_val,
    notes=notes_val,
    name_en=name_en_val,
  )


def _sync_clients_if_enabled(sync_clients_bidirectional) -> None:
  try:
    if (
      sync_clients_bidirectional
      and current_app.config.get("INVOICEAPP_CLIENT_SYNC_ENABLED")
      and current_app.config.get("INVOICEAPP_INTEGRATED")
      and not unified_clients_enabled()
    ):
      sync_clients_bidirectional(current_app.config.get("DB_PATH") or "")
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoice_create.sync_clients_bidirectional",
      log_key="billing_invoices.invoice_create.sync_clients_bidirectional",
      log_window_seconds=300,
    )


def _normalize_items_for_save(
  submitted_items: list[dict], foreign_vat_bases: list[str]
) -> list[dict[str, Any]]:
  items = []
  for idx, item in enumerate(submitted_items):
    try:
      fv = 0
      if item["item_type"] == "foreign" and idx < len(foreign_vat_bases):
        fv = 1 if (foreign_vat_bases[idx] == "1") else 0
    except Exception:
      fv = 0
    is_taxable = (
      1
      if item["item_type"] == "service"
      else (1 if (item["item_type"] == "foreign" and fv == 1) else 0)
    )
    fx_fee = d(item.get("fx_fee") or "0") if item.get("fx_fee") else Decimal("0")
    fx_gov = d(item.get("fx_gov") or "0") if item.get("fx_gov") else Decimal("0")
    fx_markup = d(item.get("fx_markup") or "3") if item.get("fx_markup") else Decimal("3")
    fx_rate_used = d(item.get("fx_rate_used")) if item.get("fx_rate_used") else None
    fx_ok = item["item_type"] == "foreign"
    items.append(
      {
        "description": item["description"],
        "qty": d(item["qty"]),
        "unit_price": d(item["unit_price"]),
        "item_type": item["item_type"],
        "discount": d(item["discount"]),
        "is_taxable": is_taxable,
        "phase": item.get("phase", "app"),
        "fx_currency": (item.get("fx_currency") if fx_ok else None),
        "fx_fee": (fx_fee if fx_ok else None),
        "fx_gov": (fx_gov if fx_ok else None),
        "fx_markup": (fx_markup if fx_ok else None),
        "fx_rate_used": (fx_rate_used if fx_ok else None),
        "is_estimated": item.get("is_estimated", 0),
      }
    )
  return items


def _find_duplicate_previous_invoice(
  conn, *, client_id: int, currency: str, total_minor: int, item_count: int
):
  try:
    last_row = conn.execute(
      "SELECT id, client_id, total_minor, currency, number FROM invoices ORDER BY id DESC LIMIT 1"
    ).fetchone()
  except Exception:
    last_row = None
  if not last_row:
    return None

  try:
    same_client = int(last_row["client_id"]) == int(client_id)
  except Exception:
    same_client = False
  try:
    last_currency = (last_row["currency"] or "").upper()
  except Exception:
    last_currency = ""
  same_currency = last_currency == (currency or "").upper()
  try:
    last_total_minor = int(last_row["total_minor"] or 0)
  except Exception:
    last_total_minor = None
  same_total = last_total_minor is not None and int(last_total_minor) == int(total_minor or 0)
  if not (same_client and same_currency and same_total):
    return None

  try:
    last_count = (
      conn.execute(
        "SELECT COUNT(*) FROM line_items WHERE invoice_id=?",
        (last_row["id"],),
      ).fetchone()[0]
      or 0
    )
  except Exception:
    last_count = 0
  if last_count == item_count:
    return last_row["number"]
  return None


def _validate_deposit_request(
  state: InvoiceCreatePageState,
  form,
  submitted_items: list[dict],
  settlement_splits: list[dict] | None,
  *,
  business_profile_id: int,
  client_id: int,
  currency: str,
  total_minor: int,
  status_code: str,
  deposit_amount_raw: str,
  parse_amount_to_minor: Callable[[str, str], int],
) -> tuple[int | None, str, bool]:
  use_deposit = (form.get("use_deposit") or "").strip() == "1"
  if not use_deposit:
    return None, status_code, False

  auto_published_for_deposit = False
  status_norm = (status_code or "draft").strip().lower()
  if status_norm == "draft":
    status_code = "sent"
    status_norm = "sent"
    auto_published_for_deposit = True
    flash("Status was changed to issued so the retainer can be applied.", "warning")

  def _error(message: str):
    flash(message, "error")
    raise _InvoiceCreateRenderError(
      _render_validation_error(
        state,
        form,
        submitted_items,
        settlement_splits,
        business_profile_id=business_profile_id,
        client_id=client_id,
        status_code=status_code,
      )
    )

  if status_norm == "void":
    _error("Retainers cannot be applied to void invoices. Change the status first.")

  try:
    outstanding_minor = int(total_minor or 0)
  except Exception:
    outstanding_minor = 0
  if outstanding_minor <= 0:
    _error("There is no balance available for retainer application.")

  try:
    if deposit_amount_raw:
      req_minor = abs(int(parse_amount_to_minor(deposit_amount_raw, currency)))
    else:
      req_minor = int(outstanding_minor)
  except Exception:
    _error("Invalid retainer amount format.")

  if req_minor <= 0:
    _error("Invalid retainer amount.")
  if req_minor > outstanding_minor:
    req_minor = int(outstanding_minor)

  try:
    bal_bp = get_client_deposit_balance_minor(
      state.conn, business_profile_id, client_id, currency
    )
  except Exception:
    bal_bp = 0
  try:
    bal_global = get_client_deposit_balance_minor(state.conn, None, client_id, currency)
  except Exception:
    bal_global = 0

  available = int(bal_bp) + int(bal_global)
  if not deposit_amount_raw:
    req_minor = min(int(req_minor), int(available))
  elif int(req_minor) > int(available):
    _error("Insufficient retainer balance.")
  if int(req_minor) <= 0:
    _error("Insufficient retainer balance.")

  return int(req_minor), status_code, auto_published_for_deposit


def _persist_invoice_header_and_items(
  state: InvoiceCreatePageState,
  hooks: InvoiceCreateHooks,
  *,
  client_id: int,
  business_profile_id: int,
  number: str,
  internal_reference: str,
  issue_date: str | None,
  due_date: str | None,
  status_code: str,
  notes: str | None,
  invoice_language: str,
  subtotal: Decimal,
  tax: Decimal,
  total: Decimal,
  subtotal_minor: int,
  tax_minor: int,
  total_minor: int,
  currency: str,
  vat_rate: Decimal,
  bp_current: dict,
  settlement_meta: str | None,
  items: list[dict[str, Any]],
) -> int:
  conn = state.conn
  inv_id = None
  try:
    if not conn.in_transaction:
      conn.execute("BEGIN IMMEDIATE")

    billing_status, payment_status = hooks.compute_billing_payment_from_status(status_code, 0)
    legacy_status = hooks.derive_legacy_status_from_split(billing_status, payment_status)

    try:
      invoice_cols = _get_column_names(conn, "invoices")
    except Exception:
      invoice_cols = set()

    header_columns = [
      "client_id",
      "business_profile_id",
      "number",
      "internal_reference",
      "issue_date",
      "due_date",
      "status",
      "billing_status",
      "payment_status",
      "notes",
      "subtotal",
      "tax",
      "total",
      "subtotal_minor",
      "tax_minor",
      "total_minor",
      "currency",
      "vat_rate",
      "business_snapshot",
      "settlement_meta",
    ]
    header_values = [
      client_id,
      business_profile_id,
      number,
      internal_reference,
      issue_date,
      due_date,
      legacy_status,
      billing_status,
      payment_status,
      notes,
      float(subtotal),
      float(tax),
      float(total),
      subtotal_minor,
      tax_minor,
      total_minor,
      currency,
      float(vat_rate),
      snapshot_of_profile(bp_current),
      settlement_meta,
    ]
    if "language" in invoice_cols:
      header_columns.append("language")
      header_values.append(invoice_language)
    placeholders = ",".join("?" for _ in header_columns)
    inv_id = _execute_insert_returning_id(
      conn,
      f"INSERT INTO invoices ({', '.join(header_columns)}) VALUES ({placeholders})",
      tuple(header_values),
    )

    if not inv_id:
      fetched_row = conn.execute(
        "SELECT id FROM invoices WHERE business_profile_id=? AND number=?",
        (int(business_profile_id), str(number)),
      ).fetchone()
      if fetched_row:
        inv_id = row_get(fetched_row, "id", 0)
    if not inv_id:
      raise RuntimeError("failed to obtain invoice id after insert")

    try:
      cols = _get_column_names(conn, "line_items")
    except Exception:
      cols = set()
    has_fx_cols = all(
      column in cols
      for column in ("fx_currency", "fx_fee", "fx_gov", "fx_markup", "fx_rate_used")
    )
    has_phase_col = "phase" in cols
    has_estimated_col = "is_estimated" in cols

    cur = conn.cursor()
    for item in items:
      qty_minor = to_minor(item["qty"], currency)
      unit_price_minor = to_minor(item["unit_price"], currency)

      if has_phase_col and has_fx_cols and has_estimated_col:
        cur.execute(
          """INSERT INTO line_items
            (invoice_id, description, qty, unit_price, qty_minor, unit_price_minor,
            item_type, discount, is_taxable, phase, fx_currency, fx_fee, fx_gov, fx_markup, fx_rate_used, is_estimated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (
            inv_id,
            item["description"],
            float(item["qty"]),
            float(item["unit_price"]),
            qty_minor,
            unit_price_minor,
            item["item_type"],
            float(item["discount"]),
            item["is_taxable"],
            item.get("phase") or "app",
            item.get("fx_currency"),
            float(item.get("fx_fee") or 0),
            float(item.get("fx_gov") or 0),
            float(item.get("fx_markup") or 0),
            (
              float(item.get("fx_rate_used"))
              if item.get("fx_rate_used") is not None
              else None
            ),
            item.get("is_estimated", 0),
          ),
        )
      elif has_phase_col and has_fx_cols:
        cur.execute(
          """INSERT INTO line_items
            (invoice_id, description, qty, unit_price, qty_minor, unit_price_minor,
            item_type, discount, is_taxable, phase, fx_currency, fx_fee, fx_gov, fx_markup, fx_rate_used)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (
            inv_id,
            item["description"],
            float(item["qty"]),
            float(item["unit_price"]),
            qty_minor,
            unit_price_minor,
            item["item_type"],
            float(item["discount"]),
            item["is_taxable"],
            item.get("phase") or "app",
            item.get("fx_currency"),
            float(item.get("fx_fee") or 0),
            float(item.get("fx_gov") or 0),
            float(item.get("fx_markup") or 0),
            (
              float(item.get("fx_rate_used"))
              if item.get("fx_rate_used") is not None
              else None
            ),
          ),
        )
      elif has_phase_col and not has_fx_cols:
        cur.execute(
          """INSERT INTO line_items
            (invoice_id, description, qty, unit_price, qty_minor, unit_price_minor,
            item_type, discount, is_taxable, phase)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
          (
            inv_id,
            item["description"],
            float(item["qty"]),
            float(item["unit_price"]),
            qty_minor,
            unit_price_minor,
            item["item_type"],
            float(item["discount"]),
            item["is_taxable"],
            item.get("phase") or "app",
          ),
        )
      elif has_fx_cols:
        cur.execute(
          """INSERT INTO line_items
            (invoice_id, description, qty, unit_price, qty_minor, unit_price_minor,
            item_type, discount, is_taxable, fx_currency, fx_fee, fx_gov, fx_markup, fx_rate_used)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (
            inv_id,
            item["description"],
            float(item["qty"]),
            float(item["unit_price"]),
            qty_minor,
            unit_price_minor,
            item["item_type"],
            float(item["discount"]),
            item["is_taxable"],
            item.get("fx_currency"),
            float(item.get("fx_fee") or 0),
            float(item.get("fx_gov") or 0),
            float(item.get("fx_markup") or 0),
            (
              float(item.get("fx_rate_used"))
              if item.get("fx_rate_used") is not None
              else None
            ),
          ),
        )
      else:
        cur.execute(
          """INSERT INTO line_items
            (invoice_id, description, qty, unit_price, qty_minor, unit_price_minor,
            item_type, discount, is_taxable)
            VALUES (?,?,?,?,?,?,?,?,?)""",
          (
            inv_id,
            item["description"],
            float(item["qty"]),
            float(item["unit_price"]),
            qty_minor,
            unit_price_minor,
            item["item_type"],
            float(item["discount"]),
            item["is_taxable"],
          ),
        )

    try:
      inv_cols = _get_column_names(conn, "invoices")
    except Exception:
      inv_cols = set()
    if "is_outgoing" in inv_cols:
      conn.execute(
        "UPDATE invoices SET is_outgoing=? WHERE id=?",
        (1 if state.outgoing_mode else 0, int(inv_id)),
      )

    conn.commit()
    return int(inv_id)
  except Exception as exc:
    try:
      conn.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="billing_invoices.invoice_create.persist.rollback",
        log_key="billing_invoices.invoice_create.persist.rollback",
        log_window_seconds=300,
      )
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoice_create.persist",
      log_key="billing_invoices.invoice_create.persist",
      log_window_seconds=300,
    )
    abort(500, "An error occurred while saving data.")


def _link_invoice_to_case(
  state: InvoiceCreatePageState,
  hooks: InvoiceCreateHooks,
  *,
  invoice_id: int,
  ipm_case_id: str | None,
  ipm_case_ref: str | None,
  internal_reference: str | None,
) -> None:
  conn = state.conn
  try:
    raw_case_id = str(ipm_case_id or "").strip()
    raw_case_ref = str(ipm_case_ref or "").strip()
    raw_internal_ref = str(internal_reference or "").strip()
    explicit_case_input = bool(raw_case_id or raw_case_ref)

    if explicit_case_input:
      conn.execute(
        "UPDATE invoices SET ipm_case_id=?, ipm_case_ref=? WHERE id=?",
        (raw_case_id or None, raw_case_ref or None, int(invoice_id)),
      )

    link_result = hooks.resolve_invoice_case_target(
      conn,
      ipm_case_id=raw_case_id,
      ipm_case_ref=raw_case_ref,
      internal_reference=raw_internal_ref,
    )

    if link_result.get("status") == "ok":
      matter_id_to_link = str(link_result.get("matter_id") or "").strip()
      our_ref_to_link = str(link_result.get("our_ref") or "").strip() or matter_id_to_link
      conn.execute(
        "UPDATE invoices SET ipm_case_id=?, ipm_case_ref=? WHERE id=?",
        (matter_id_to_link, our_ref_to_link, int(invoice_id)),
      )
      conn.execute(
        "INSERT INTO external_invoice_case_map(matter_id, our_ref, external_invoice_id) VALUES (?,?,?) ON CONFLICT DO NOTHING",
        (matter_id_to_link, our_ref_to_link, int(invoice_id)),
      )
    elif link_result.get("status") == "ambiguous":
      matches = link_result.get("matches") or []
      sample = ", ".join(
        f"{(match.get('our_ref') or match.get('matter_id') or '-')}({match.get('matter_id')})"
        for match in matches[:3]
      )
      if link_result.get("source") == "internal_reference":
        msg = "Automatic linking was skipped because the internal reference matched multiple matters."
      else:
        msg = "Automatic linking was skipped because the matter identifier matched multiple matters."
      if sample:
        msg = f"{msg} Candidates: {sample}"
      flash(msg, "warning")
    elif link_result.get("status") == "not_found" and explicit_case_input:
      flash(
        "Automatic matter linking was skipped because the matter could not be found. Check Matter ID, Our Ref, former Our Ref, or Your Ref.",
        "warning",
      )
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoice_create.case_linking",
      log_key="billing_invoices.invoice_create.case_linking",
      log_window_seconds=300,
    )


def _apply_deposit_if_requested(
  state: InvoiceCreatePageState,
  hooks: InvoiceCreateHooks,
  *,
  invoice_id: int,
  invoice_number: str,
  client_id: int,
  business_profile_id: int,
  currency: str,
  total_minor: int,
  deposit_apply_minor: int | None,
  deposit_amount_raw: str,
  deposit_memo_raw: str | None,
) -> None:
  if deposit_apply_minor is None or deposit_apply_minor <= 0:
    return

  conn = state.conn
  applied_entries = []
  try:
    conn.execute("BEGIN IMMEDIATE")
    user = get_current_user()
    memo = deposit_memo_raw or f"invoice:{invoice_number}"

    bal_bp = get_client_deposit_balance_minor(conn, business_profile_id, client_id, currency)
    bal_global = get_client_deposit_balance_minor(conn, None, client_id, currency)
    available = int(bal_bp) + int(bal_global)

    req_minor = int(deposit_apply_minor)
    if not deposit_amount_raw:
      req_minor = min(int(req_minor), int(available))
    elif int(req_minor) > int(available):
      raise ValueError("Insufficient retainer balance.")
    if req_minor <= 0:
      raise ValueError("Insufficient retainer balance.")

    use_bp = min(int(req_minor), int(bal_bp))
    use_global = int(req_minor) - int(use_bp)
    if int(use_global) > int(bal_global):
      raise ValueError("Insufficient retainer balance.")

    if use_bp > 0:
      res = insert_client_deposit_ledger_entry(
        conn,
        business_profile_id,
        client_id,
        currency,
        -int(use_bp),
        "apply",
        memo=memo,
        related_invoice_id=int(invoice_id),
        created_by=(user["id"] if user else None),
        begin_immediate=False,
        commit_if_started=False,
      )
      applied_entries.append(
        {
          "entry_id": res.get("entry_id"),
          "business_profile_id": int(business_profile_id),
          "amount_minor": -int(use_bp),
          "balance_before_minor": res.get("balance_before_minor"),
          "balance_after_minor": res.get("balance_after_minor"),
          "memo": memo,
        }
      )

    if use_global > 0:
      res = insert_client_deposit_ledger_entry(
        conn,
        None,
        client_id,
        currency,
        -int(use_global),
        "apply",
        memo=memo,
        related_invoice_id=int(invoice_id),
        created_by=(user["id"] if user else None),
        begin_immediate=False,
        commit_if_started=False,
      )
      applied_entries.append(
        {
          "entry_id": res.get("entry_id"),
          "business_profile_id": None,
          "amount_minor": -int(use_global),
          "balance_before_minor": res.get("balance_before_minor"),
          "balance_after_minor": res.get("balance_after_minor"),
          "memo": memo,
        }
      )

    applied_total_minor = int(use_bp) + int(use_global)
    new_outstanding = int(total_minor or 0) - int(applied_total_minor)
    new_payment_status = "paid" if new_outstanding <= 0 else "pending"
    new_payment_verified = 1 if new_outstanding <= 0 else 0

    meta = {}
    if new_payment_verified == 1:
      meta["verified_by_user_id"] = user["id"] if user else None
      meta["verified_by_username"] = user["username"] if user else None
      meta["verified_at"] = datetime.now().isoformat(timespec="seconds")
      meta["verified_via"] = "deposit"

    conn.execute(
      "UPDATE invoices SET payment_status=?, payment_verified=?, payment_meta=? WHERE id=?",
      (
        new_payment_status,
        new_payment_verified,
        json.dumps(meta, ensure_ascii=False),
        int(invoice_id),
      ),
    )
    hooks.sync_legacy_status(conn, int(invoice_id))
    conn.commit()
  except Exception as exc:
    try:
      conn.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="billing_invoices.invoice_create.apply_deposit.rollback",
        log_key="billing_invoices.invoice_create.apply_deposit.rollback",
        log_window_seconds=300,
      )
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoice_create.apply_deposit",
      log_key="billing_invoices.invoice_create.apply_deposit",
      log_window_seconds=300,
    )
    flash("Failed to apply the retainer. The invoice was created.", "error")
    return

  for entry in applied_entries:
    try:
      audit_meta = build_client_deposit_audit_meta(
        entry_id=entry.get("entry_id"),
        business_profile_id=entry.get("business_profile_id"),
        client_id=client_id,
        currency=currency,
        amount_minor=entry.get("amount_minor"),
        entry_type="apply",
        memo=entry.get("memo"),
        related_invoice_id=int(invoice_id),
        balance_before_minor=entry.get("balance_before_minor"),
        balance_after_minor=entry.get("balance_after_minor"),
      )
      log_audit("invoice.deposit.apply", "invoice", int(invoice_id), audit_meta)
    except Exception as log_exc:
      report_swallowed_exception(
        log_exc,
        context="billing_invoices.invoice_create.apply_deposit.audit",
        log_key="billing_invoices.invoice_create.apply_deposit.audit",
        log_window_seconds=300,
      )
  flash("Retainer applied.", "success")


def _log_invoice_create_audit(
  *,
  invoice_id: int,
  invoice_number: str,
  client_id: int,
  total: Decimal,
  currency: str,
  auto_published_for_deposit: bool,
) -> None:
  try:
    user = get_current_user()
    created_by = {
      "created_by_user_id": (
        (user.get("id") if hasattr(user, "get") else user["id"]) if user else None
      ),
      "created_by_username": (
        (user.get("username") if hasattr(user, "get") else user["username"])
        if user
        else None
      ),
    }
  except Exception:
    created_by = {"created_by_user_id": None, "created_by_username": None}

  meta_obj = {
    "number": invoice_number,
    "client_id": client_id,
    "total": float(total),
    "currency": str(currency),
    **created_by,
  }
  try:
    meta_str = json.dumps(meta_obj, ensure_ascii=False)
  except Exception:
    meta_str = None
  log_audit("invoice.create", "invoice", invoice_id, meta_str)

  if auto_published_for_deposit:
    try:
      log_audit(
        "invoice.publish",
        "invoice",
        int(invoice_id),
        '{"to_status":"sent","via":"deposit_create"}',
      )
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoice_create.auto_publish.audit",
        log_key="billing_invoices.invoice_create.auto_publish.audit",
        log_window_seconds=300,
      )


class _InvoiceCreateRenderError(Exception):
  def __init__(self, response) -> None:
    super().__init__("invoice create requires form re-render")
    self.response = response


def handle_invoice_create_submission(
  state: InvoiceCreatePageState,
  hooks: InvoiceCreateHooks,
  *,
  sync_clients_bidirectional=None,
):
  form = request.form
  state.outgoing_mode = _submitted_outgoing_mode(form, default=state.outgoing_mode)
  business_profile_id = hooks.safe_int(form.get("business_profile_id", 1), 1, 1, None)
  client_id_str = form.get("client_id")
  ipm_case_id = (form.get("ipm_case_id") or "").strip() or None
  ipm_case_ref = (form.get("ipm_case_ref") or "").strip() or None
  deposit_amount_raw = (form.get("deposit_amount") or "").strip()
  deposit_memo_raw = (form.get("deposit_memo") or "").strip() or None
  confirm_duplicate = form.get("confirm_duplicate") == "1"

  submitted_items, foreign_vat_bases = _collect_submitted_items(form)
  settlement_splits, settlement_meta, settlement_error = hooks.parse_settlement_splits(
    form,
    allowed_bp_ids=_allowed_settlement_bp_ids(state.all_profiles, hooks.safe_int),
  )
  if settlement_error:
    flash(settlement_error, "error")
    return _render_validation_error(
      state,
      form,
      submitted_items,
      settlement_splits,
      business_profile_id=business_profile_id,
    )

  try:
    client_id = _resolve_or_create_client(
      state.conn,
      form,
      client_id_str=client_id_str,
      business_profile_id=business_profile_id,
      submitted_items=submitted_items,
      settlement_splits=settlement_splits,
      state=state,
      safe_int=hooks.safe_int,
    )
  except _InvoiceCreateRenderError as exc:
    return exc.response

  _sync_clients_if_enabled(sync_clients_bidirectional)

  number = (form.get("number") or "").strip()
  internal_reference = (form.get("internal_reference") or "").strip()
  issue_date = hooks.normalize_invoice_date(form.get("issue_date"))
  due_date = hooks.normalize_invoice_date(form.get("due_date"))
  status_code = form.get("status") or "draft"
  notes = form.get("notes")
  invoice_language = form.get("invoice_language") or "en"

  items = _normalize_items_for_save(submitted_items, foreign_vat_bases)

  if str(status_code or "").strip().lower() in {"tax_issued", "cash_issued", "processed"}:
    flash("Tax-recorded status is applied only after tax documentation is confirmed.", "error")
    return _render_validation_error(
      state,
      form,
      submitted_items,
      settlement_splits,
      business_profile_id=business_profile_id,
      client_id=client_id,
      issue_date=issue_date,
      due_date=due_date,
      status_code="sent",
    )

  try:
    issue_dt = date.fromisoformat(str(issue_date)) if issue_date else None
    due_dt = date.fromisoformat(str(due_date)) if due_date else None
  except Exception:
    issue_dt = None
    due_dt = None
  if issue_dt and due_dt and issue_dt > due_dt:
    flash("Due date must be on or after the issue date.", "error")
    return _render_validation_error(
      state,
      form,
      submitted_items,
      settlement_splits,
      business_profile_id=business_profile_id,
      client_id=client_id,
      issue_date=issue_date,
      due_date=due_date,
      status_code=status_code,
    )

  bp_current = get_business_profile(business_profile_id)
  currency = bp_current["currency"] or "USD"
  vat_rate = Decimal(bp_current["vat_rate"])
  subtotal, tax, total = compute_totals(items, vat_rate)
  subtotal_minor, tax_minor, total_minor = compute_totals_minor(items, vat_rate, currency)

  duplicate_prev_number = None
  if not confirm_duplicate:
    duplicate_prev_number = _find_duplicate_previous_invoice(
      state.conn,
      client_id=client_id,
      currency=currency,
      total_minor=total_minor,
      item_count=len(items),
    )
  if duplicate_prev_number and not confirm_duplicate:
    flash(
      f"The recently created invoice ({duplicate_prev_number}) has the same client and total. Confirm this is not a duplicate, then save again.",
      "warning",
    )
    return _render_validation_error(
      state,
      form,
      submitted_items,
      settlement_splits,
      business_profile_id=business_profile_id,
      client_id=client_id,
      issue_date=issue_date,
      due_date=due_date,
      status_code=status_code,
      duplicate_warning=True,
      duplicate_prev_number=duplicate_prev_number,
    )

  try:
    deposit_apply_minor, status_code, auto_published_for_deposit = _validate_deposit_request(
      state,
      form,
      submitted_items,
      settlement_splits,
      business_profile_id=business_profile_id,
      client_id=client_id,
      currency=currency,
      total_minor=total_minor,
      status_code=status_code,
      deposit_amount_raw=deposit_amount_raw,
      parse_amount_to_minor=hooks.parse_amount_to_minor,
    )
  except _InvoiceCreateRenderError as exc:
    return exc.response

  if not number:
    today_str = datetime.now(
      ZoneInfo(current_app.config.get("TIMEZONE", "America/New_York"))
    ).strftime("%Y%m%d")
    number = next_invoice_number(state.conn, business_profile_id, f"INV-{today_str}-")

  invoice_id = _persist_invoice_header_and_items(
    state,
    hooks,
    client_id=client_id,
    business_profile_id=business_profile_id,
    number=number,
    internal_reference=internal_reference,
    issue_date=issue_date,
    due_date=due_date,
    status_code=status_code,
    notes=notes,
    invoice_language=invoice_language,
    subtotal=subtotal,
    tax=tax,
    total=total,
    subtotal_minor=subtotal_minor,
    tax_minor=tax_minor,
    total_minor=total_minor,
    currency=currency,
    vat_rate=vat_rate,
    bp_current=bp_current,
    settlement_meta=settlement_meta,
    items=items,
  )

  _link_invoice_to_case(
    state,
    hooks,
    invoice_id=invoice_id,
    ipm_case_id=ipm_case_id,
    ipm_case_ref=ipm_case_ref,
    internal_reference=internal_reference,
  )
  _apply_deposit_if_requested(
    state,
    hooks,
    invoice_id=invoice_id,
    invoice_number=number,
    client_id=client_id,
    business_profile_id=business_profile_id,
    currency=currency,
    total_minor=total_minor,
    deposit_apply_minor=deposit_apply_minor,
    deposit_amount_raw=deposit_amount_raw,
    deposit_memo_raw=deposit_memo_raw,
  )
  _log_invoice_create_audit(
    invoice_id=invoice_id,
    invoice_number=number,
    client_id=client_id,
    total=total,
    currency=currency,
    auto_published_for_deposit=auto_published_for_deposit,
  )
  return redirect(
    url_for(
      "billing_invoices.invoices.view_invoice",
      invoice_id=invoice_id,
      lang=invoice_language,
      outgoing="1" if state.outgoing_mode else "0",
    )
  )
