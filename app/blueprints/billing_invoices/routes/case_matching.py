from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any

from flask import Blueprint, abort, current_app, jsonify, render_template, request

from app.services.billing.utils import is_compact_query, sql_ci_contains_any, to_compact

from ..auth import role_required
from ..db import _is_postgres, _table_exists, get_db
from ..repos.invoice_case_repo import link_case_to_invoice as repo_link_case_to_invoice
from ..repos.invoice_case_repo import unlink_case_from_invoice as repo_unlink_case_from_invoice

bp = Blueprint("case_matching", __name__)


def _not_deleted_sql(column_expr: str) -> str:
  return (
    f"COALESCE(LOWER(CAST({column_expr} AS TEXT)), 'false') "
    "NOT IN ('1', 'true', 't', 'yes', 'y')"
  )


_CASE_REC_SCORE_THRESHOLD = 58
_CASE_REC_MIN_ANCHOR_SCORE = 35
_COMPANY_TOKENS = (
  "Company",
  "Company",
  "Company",
  "peopleCompany",
  "",
  "",
  "Company",
  "",
  "",
  "()",
  "㈜",
  "()",
  "()",
  "()",
  "corp",
  "corporation",
  "inc",
  "co",
  "ltd",
  "limited",
  "llc",
)


def _ensure_external_invoice_case_map(conn) -> None:
  if _table_exists(conn, "external_invoice_case_map"):
    return
  current_app.logger.error(
    "external_invoice_case_map table missing; apply migrations before linking invoices"
  )
  abort(500, "external_invoice_case_map table missing")


def _parse_int(v, default: int) -> int:
  try:
    return int(v)
  except Exception:
    return default


def _clamp_page_for_total(page: int, total: int, per_page: int) -> tuple[int, int]:
  page_count = max(1, (int(total or 0) + int(per_page or 1) - 1) // int(per_page or 1))
  return min(max(int(page or 1), 1), page_count), page_count


def _safe_normalize(value: str | None) -> str:
  raw = unicodedata.normalize("NFKC", str(value or ""))
  raw = re.sub(r"[|/\\,.;:_-]+", " ", raw.lower())
  return re.sub(r"\s+", " ", raw).strip()


def _remove_company_tokens(value: str | None) -> str:
  out = _safe_normalize(value)
  for token in _COMPANY_TOKENS:
    out = out.replace(token, " ")
  out = re.sub(r"\b(?:co|corp|inc|ltd|llc)\b", " ", out)
  return re.sub(r"\s+", " ", out).strip()


def _compact_match_text(value: str | None) -> str:
  return re.sub(r"[^0-9a-z-]+", "", _remove_company_tokens(value))


def _token_set(value: str | None) -> set[str]:
  out: set[str] = set()
  for part in _remove_company_tokens(value).split():
    token = re.sub(r"[^0-9a-z-]+", "", part).strip()
    if len(token) >= 2:
      out.add(token)
  compact = _compact_match_text(value)
  if len(compact) >= 2:
    out.add(compact)
  return out


def _bigram_similarity(a: str | None, b: str | None) -> float:
  x = _compact_match_text(a)
  y = _compact_match_text(b)
  if not x or not y:
    return 0.0
  if x == y:
    return 1.0
  if len(x) < 2 or len(y) < 2:
    return 0.0
  xb = {x[i : i + 2] for i in range(len(x) - 1)}
  yb = {y[i : i + 2] for i in range(len(y) - 1)}
  if not xb or not yb:
    return 0.0
  return len(xb & yb) / max(len(xb), len(yb))


def _token_overlap_ratio(a: str | None, b: str | None) -> float:
  sa = _token_set(a)
  sb = _token_set(b)
  if not sa or not sb:
    return 0.0
  return len(sa & sb) / min(len(sa), len(sb))


def _party_name_score(case_client: str | None, invoice_client: str | None) -> tuple[int, str]:
  case_core = _compact_match_text(case_client)
  invoice_core = _compact_match_text(invoice_client)
  if not case_core or not invoice_core:
    return 0, ""
  if case_core == invoice_core:
    return 44, "Client match"
  shorter = min(len(case_core), len(invoice_core))
  if shorter >= 3 and (case_core in invoice_core or invoice_core in case_core):
    return 36, "Client match"
  overlap = _token_overlap_ratio(case_client, invoice_client)
  if overlap >= 1:
    return 32, "Client match"
  if overlap >= 0.5:
    return 24, "Client days match"
  similarity = _bigram_similarity(case_core, invoice_core)
  if similarity >= 0.72:
    return 18, "Client "
  if similarity >= 0.56:
    return 10, "Client "
  return 0, ""


def _compact_contains(haystack: str | None, needle: str | None, *, min_len: int = 4) -> bool:
  h = _compact_match_text(haystack)
  n = _compact_match_text(needle)
  return bool(h and n and len(n) >= min_len and n in h)


def _compact_equal(a: str | None, b: str | None) -> bool:
  x = _compact_match_text(a)
  y = _compact_match_text(b)
  return bool(x and y and x == y)


def _invoice_recommend_text(invoice: dict[str, Any]) -> str:
  return " ".join(
    str(invoice.get(key) or "")
    for key in (
      "number",
      "internal_reference",
      "ipm_case_id",
      "ipm_case_ref",
      "client_name",
      "biz_name",
      "line_item_text",
    )
  )


def _identifier_score(invoice: dict[str, Any], case: dict[str, Any]) -> tuple[int, str]:
  matter_id = str(case.get("matter_id") or "").strip()
  our_ref = str(case.get("our_ref") or "").strip()
  invoice_text = _invoice_recommend_text(invoice)

  candidates: list[tuple[int, str]] = []
  if matter_id and _compact_equal(matter_id, str(invoice.get("ipm_case_id") or "")):
    candidates.append((110, "Matter ID match"))
  if our_ref and _compact_equal(our_ref, str(invoice.get("ipm_case_ref") or "")):
    candidates.append((105, "Our Ref match"))
  if our_ref and _compact_equal(our_ref, str(invoice.get("internal_reference") or "")):
    candidates.append((100, "Internal match"))
  if our_ref and _compact_contains(invoice_text, our_ref, min_len=4):
    candidates.append((90, "Our Ref contains"))
  if matter_id and _compact_contains(invoice_text, matter_id, min_len=6):
    candidates.append((82, "Matter ID contains"))
  return max(candidates, default=(0, ""), key=lambda item: item[0])


def _title_score(invoice: dict[str, Any], case: dict[str, Any]) -> tuple[int, str]:
  title = str(case.get("right_name") or "").strip()
  if not title:
    return 0, ""
  invoice_text = _invoice_recommend_text(invoice)
  title_compact = _compact_match_text(title)
  if len(title_compact) >= 4 and _compact_contains(invoice_text, title, min_len=4):
    return 36, "Title contains"
  overlap = _token_overlap_ratio(title, invoice_text)
  if overlap >= 0.8:
    return 28, "Title match"
  if overlap >= 0.5:
    return 18, "Title partial match"
  similarity = _bigram_similarity(title, invoice_text)
  if similarity >= 0.64:
    return 16, "Title similar"
  return 0, ""


def _parse_match_date(value: str | None) -> datetime | None:
  raw = str(value or "").strip()
  if not raw:
    return None
  raw = raw[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", raw) else raw
  for fmt in ("%Y-%m-%d", "%Y%m%d"):
    try:
      return datetime.strptime(raw[: len(datetime.now().strftime(fmt))], fmt)
    except Exception:
      continue
  try:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
  except Exception:
    return None


def _case_invoice_date_score(invoice: dict[str, Any], case: dict[str, Any]) -> tuple[int, str]:
  issue_dt = _parse_match_date(str(invoice.get("issue_date") or ""))
  case_dt = _parse_match_date(
    str(case.get("entered_at") or case.get("retained_at") or case.get("created_at") or "")
  )
  if issue_dt is None or case_dt is None:
    return 0, ""
  diff = (issue_dt.date() - case_dt.date()).days
  if 0 <= diff <= 120:
    return 8, "Matter Issued"
  if 121 <= diff <= 365:
    return 5, "Matter 1 Issued"
  if -30 <= diff < 0:
    return 4, " Issued"
  return 0, ""


def score_invoice_case_recommendation(
  *, invoice: dict[str, Any], case: dict[str, Any]
) -> dict[str, Any]:
  """Score whether an invoice and a matter look like the same billing target."""
  ref_score, ref_reason = _identifier_score(invoice, case)
  title_score, title_reason = _title_score(invoice, case)
  client_score, client_reason = _party_name_score(
    case.get("client_name"),
    invoice.get("client_name"),
  )
  date_score, date_reason = _case_invoice_date_score(invoice, case)
  total = ref_score + title_score + client_score + date_score
  anchor_score = ref_score + title_score
  recommended = total >= _CASE_REC_SCORE_THRESHOLD and (
    ref_score >= 80
    or anchor_score >= _CASE_REC_MIN_ANCHOR_SCORE
    or (title_score >= 18 and client_score >= 24)
  )
  return {
    "score": total,
    "reasons": [
      reason for reason in (ref_reason, title_reason, client_reason, date_reason) if reason
    ],
    "recommended": recommended,
  }


def _has_matter_overview(conn) -> bool:
  try:
    conn.execute("SELECT 1 FROM v_matter_overview LIMIT 1").fetchone()
    return True
  except Exception:
    return False


def _case_client_sql_parts(conn) -> tuple[str, str]:
  is_postgres = _is_postgres(conn)
  has_matter_overview = _has_matter_overview(conn)
  if is_postgres:
    custom_field_party_value_expr = (
      "COALESCE("
      "NULLIF(BTRIM(cf.data->>'client_name'), ''), "
      "NULLIF(BTRIM(cf.data->>'applicant_name'), ''), "
      "NULLIF(BTRIM(cf.data->>'application_applicant_name'), '')"
      ")"
    )
  else:
    custom_field_party_value_expr = (
      "COALESCE("
      "NULLIF(TRIM(json_extract(cf.data, '$.client_name')), ''), "
      "NULLIF(TRIM(json_extract(cf.data, '$.applicant_name')), ''), "
      "NULLIF(TRIM(json_extract(cf.data, '$.application_applicant_name')), '')"
      ")"
    )
  custom_field_client_expr = (
    f"(SELECT MAX({custom_field_party_value_expr}) "
    "FROM matter_custom_field cf WHERE cf.matter_id = m.matter_id)"
  )

  overview_join_sql = ""
  client_name_expr = (
    f"COALESCE(NULLIF(cfi.client_name, ''), NULLIF({custom_field_client_expr}, ''), '')"
  )
  if has_matter_overview:
    overview_join_sql = "LEFT JOIN v_matter_overview ov ON ov.matter_id = m.matter_id"
    client_name_expr = (
      "COALESCE("
      "NULLIF(cfi.client_name, ''), "
      "NULLIF(ov.clients, ''), "
      f"NULLIF({custom_field_client_expr}, ''), "
      "''"
      ")"
    )
  return client_name_expr, overview_join_sql


def _line_item_text_expr(conn) -> str:
  if _is_postgres(conn):
    return (
      "(SELECT COALESCE(string_agg(COALESCE(li.description, ''), ' '), '') "
      "FROM line_items li WHERE li.invoice_id = invoices.id)"
    )
  return (
    "(SELECT COALESCE(group_concat(COALESCE(li.description, ''), ' '), '') "
    "FROM line_items li WHERE li.invoice_id = invoices.id)"
  )


def _invoice_item_from_matching_row(row) -> dict[str, Any]:
  item = {
    "id": row[0],
    "number": row[1],
    "internal_reference": row[2],
    "ipm_case_id": row[3],
    "ipm_case_ref": row[4],
    "issue_date": row[5],
    "total_minor": int(row[6] or 0),
    "currency": row[7] or "USD",
    "billing_status": row[8] or "",
    "payment_status": row[9] or "",
    "client_name": row[10] or "",
    "biz_name": row[11] or "",
    "case_count": int(row[12] or 0),
    "linked": int(row[13] or 0) > 0,
    "line_item_text": "",
  }
  try:
    if len(row) > 14:
      item["line_item_text"] = row[14] or ""
  except Exception:
    item["line_item_text"] = ""
  return item


def _case_item_from_matching_row(row) -> dict[str, Any]:
  item = {
    "matter_id": row[0],
    "our_ref": row[1],
    "right_name": row[2],
    "client_name": row[3] or "",
    "matter_type": row[4],
    "retained_at": row[5] or "",
    "linked": bool(row[6]),
    "invoice_count": int(row[7] or 0),
    "case_search_text": "",
  }
  try:
    if len(row) > 8:
      item["case_search_text"] = row[8] or ""
  except Exception:
    item["case_search_text"] = ""
  return item


def _load_recommend_case_context(conn, matter_id: str | None) -> dict[str, Any] | None:
  raw = str(matter_id or "").strip()
  if not raw:
    return None
  client_name_expr, overview_join_sql = _case_client_sql_parts(conn)
  row = conn.execute(
    f"""
    SELECT m.matter_id,
        m.our_ref,
        m.right_name,
        {client_name_expr} AS client_name,
        m.matter_type,
        m.retained_at,
        0 AS linked,
        (SELECT COUNT(*) FROM external_invoice_case_map e2 WHERE e2.matter_id = m.matter_id AND {_not_deleted_sql("e2.is_deleted")}) AS invoice_count,
        COALESCE(cfi.search_text, '') AS case_search_text,
        m.entered_at,
        m.created_at
     FROM matter m
     LEFT JOIN case_flat_index cfi ON cfi.matter_id = m.matter_id
     {overview_join_sql}
     WHERE m.matter_id = ? OR m.our_ref = ?
     LIMIT 1
    """,
    (raw, raw),
  ).fetchone()
  if not row:
    return None
  item = _case_item_from_matching_row(row)
  item["entered_at"] = row[9] or ""
  item["created_at"] = row[10] or ""
  return item


def _load_recommend_invoice_context(conn, invoice_id: int | None) -> dict[str, Any] | None:
  invoice_id_int = _parse_int(invoice_id, 0)
  if invoice_id_int <= 0:
    return None
  line_item_text_expr = _line_item_text_expr(conn)
  row = conn.execute(
    f"""
    SELECT invoices.id,
        invoices.number,
        invoices.internal_reference,
        invoices.ipm_case_id,
        invoices.ipm_case_ref,
        invoices.issue_date,
        invoices.total_minor,
        invoices.currency,
        invoices.billing_status,
        invoices.payment_status,
        clients.name AS client_name,
        bp.name AS biz_name,
        (SELECT COUNT(*) FROM external_invoice_case_map e WHERE e.external_invoice_id = invoices.id AND {_not_deleted_sql("e.is_deleted")}) AS case_count,
        0 AS is_linked,
        {line_item_text_expr} AS line_item_text
     FROM invoices
     JOIN clients ON clients.id = invoices.client_id
     LEFT JOIN business_profile bp ON bp.id = invoices.business_profile_id
     WHERE invoices.id = ?
     LIMIT 1
    """,
    (invoice_id_int,),
  ).fetchone()
  return _invoice_item_from_matching_row(row) if row else None


@bp.get("")
@role_required("admin", "staff")
def page():
  return render_template("case_invoice_matching.html")


@bp.get("/links")
@role_required("admin", "staff")
def invoice_links():
  invoice_id = _parse_int(request.args.get("invoice_id"), 0)
  if invoice_id <= 0:
    abort(400, "invoice_id is required")

  conn = get_db()
  _ensure_external_invoice_case_map(conn)
  try:
    rows = conn.execute(
      f"""
      SELECT l.matter_id,
          COALESCE(m.our_ref, l.our_ref) AS our_ref,
          COALESCE(m.right_name, '') AS right_name,
          l.created_at
       FROM external_invoice_case_map l
       LEFT JOIN matter m ON m.matter_id = l.matter_id
       WHERE l.external_invoice_id=?
        AND {_not_deleted_sql("l.is_deleted")}
       ORDER BY COALESCE(m.our_ref, l.our_ref, l.matter_id) DESC, l.id ASC
      """,
      (int(invoice_id),),
    ).fetchall()
  finally:
    conn.close()

  items = [
    {
      "matter_id": r[0],
      "our_ref": r[1],
      "right_name": r[2],
      "created_at": r[3],
    }
    for r in (rows or [])
  ]
  return jsonify({"items": items})


@bp.get("/invoices")
@role_required("admin", "staff")
def matching_invoices():
  date_from = (request.args.get("date_from") or "").strip()
  date_to = (request.args.get("date_to") or "").strip()
  q = (request.args.get("q") or "").strip()
  recommend_case_id = (request.args.get("recommend_case_id") or "").strip()
  is_compact_q = q and is_compact_query(q)

  payment = (request.args.get("payment") or "").strip().lower()
  billing = (request.args.get("billing") or "").strip().lower()
  linked_only = (request.args.get("linkedOnly") or "").strip() in ("1", "true", "True")

  page = max(_parse_int(request.args.get("page"), 1), 1)
  per_page = max(1, min(_parse_int(request.args.get("perPage"), 15), 200))

  conn = get_db()
  _ensure_external_invoice_case_map(conn)
  recommend_case = _load_recommend_case_context(conn, recommend_case_id)
  if recommend_case_id and recommend_case is None:
    conn.close()
    return jsonify({"items": [], "total": 0, "pageNum": 1, "pageCount": 1})

  where = []
  params = []

  if date_from:
    where.append("invoices.issue_date >= ?")
    params.append(date_from)
  if date_to:
    where.append("invoices.issue_date <= ?")
    params.append(date_to)

  if billing:
    where.append("COALESCE(invoices.billing_status, '') = ?")
    params.append(billing)
  if payment:
    where.append("COALESCE(invoices.payment_status, '') = ?")
    params.append(payment)

  if linked_only:
    where.append(
      f"EXISTS (SELECT 1 FROM external_invoice_case_map eicm WHERE eicm.external_invoice_id = invoices.id AND {_not_deleted_sql('eicm.is_deleted')})"
    )

  # Hide already linked invoices
  hide_linked = (request.args.get("hideLinked") or "").strip() in ("1", "true", "True")
  if hide_linked:
    where.append(
      f"NOT EXISTS (SELECT 1 FROM external_invoice_case_map eicm WHERE eicm.external_invoice_id = invoices.id AND {_not_deleted_sql('eicm.is_deleted')})"
    )

  # Case-first mode: filter invoices by case_id
  case_id = (request.args.get("case_id") or "").strip()

  if q and not is_compact_q:
    search_clause, search_params = sql_ci_contains_any(
      ["invoices.number", "invoices.internal_reference", "clients.name", "bp.name"],
      q,
    )
    if search_clause:
      where.append(search_clause)
      params += search_params

  where_sql = (" WHERE " + " AND ".join(where)) if where else ""

  # Build linked subquery for case-first mode
  linked_select = ""
  select_params = list(params)
  if case_id:
    linked_select = (
      ", (SELECT COUNT(*) FROM external_invoice_case_map e2 "
      f"WHERE e2.external_invoice_id = invoices.id AND e2.matter_id = ? AND {_not_deleted_sql('e2.is_deleted')}) AS is_linked"
    )
    select_params = [case_id] + select_params
  else:
    linked_select = ", 0 AS is_linked"
  line_item_text_expr = _line_item_text_expr(conn)

  base_sql = f"""
    SELECT invoices.id,
        invoices.number,
        invoices.internal_reference,
        invoices.ipm_case_id,
        invoices.ipm_case_ref,
        invoices.issue_date,
        invoices.total_minor,
        invoices.currency,
        invoices.billing_status,
        invoices.payment_status,
        clients.name AS client_name,
        bp.name AS biz_name,
        (SELECT COUNT(*) FROM external_invoice_case_map e WHERE e.external_invoice_id = invoices.id AND {_not_deleted_sql("e.is_deleted")}) AS case_count
        {linked_select},
        {line_item_text_expr} AS line_item_text
     FROM invoices
     JOIN clients ON clients.id = invoices.client_id
     LEFT JOIN business_profile bp ON bp.id = invoices.business_profile_id
     {where_sql}
  """

  try:
    if is_compact_q or recommend_case is not None:
      # -only Search: times  Filters
      rows_all = conn.execute(
        base_sql + " ORDER BY invoices.issue_date DESC, invoices.id DESC",
        select_params,
      ).fetchall()
      q_ch = to_compact(q)
      filtered = []
      for r in rows_all or []:
        txt = " ".join(
          [
            str(r[1] or ""),
            str(r[2] or ""),
            str(r[10] or ""),
            str(r[11] or ""),
          ]
        )
        if q_ch in to_compact(txt):
          filtered.append(r)
      if not is_compact_q:
        filtered = list(rows_all or [])
      filtered_items = []
      for r in filtered:
        item = _invoice_item_from_matching_row(r)
        if recommend_case is not None:
          recommend = score_invoice_case_recommendation(
            invoice=item,
            case=recommend_case,
          )
          if not recommend["recommended"]:
            continue
          item["recommended"] = True
          item["recommend_score"] = recommend["score"]
          item["recommend_reasons"] = recommend["reasons"]
        filtered_items.append(item)
      if recommend_case is not None:
        filtered_items.sort(
          key=lambda item: (
            int(item.get("recommend_score") or 0),
            str(item.get("issue_date") or ""),
            int(item.get("id") or 0),
          ),
          reverse=True,
        )
      total = len(filtered_items)
      page, page_count = _clamp_page_for_total(page, total, per_page)
      offset = (page - 1) * per_page
      rows = []
      items = filtered_items[offset : offset + per_page]
    else:
      total = conn.execute(
        f"SELECT COUNT(*) FROM invoices JOIN clients ON clients.id = invoices.client_id LEFT JOIN business_profile bp ON bp.id = invoices.business_profile_id {where_sql}",
        params,
      ).fetchone()[0]
      page, page_count = _clamp_page_for_total(page, total, per_page)
      offset = (page - 1) * per_page
      rows = conn.execute(
        base_sql + " ORDER BY invoices.issue_date DESC, invoices.id DESC LIMIT ? OFFSET ?",
        select_params + [per_page, offset],
      ).fetchall()
      items = [_invoice_item_from_matching_row(r) for r in rows or []]
  finally:
    conn.close()

  return jsonify(
    {"items": items, "total": int(total or 0), "pageNum": page, "pageCount": page_count}
  )


@bp.get("/cases")
@role_required("admin", "staff")
def matching_cases():
  invoice_id = _parse_int(request.args.get("invoice_id"), 0)
  recommend_invoice_id = _parse_int(request.args.get("recommend_invoice_id"), 0)
  q = (request.args.get("q") or "").strip()
  is_compact_q = q and is_compact_query(q)
  linked_only = (request.args.get("linkedOnly") or "").strip() in ("1", "true", "True")
  case_date_from = (request.args.get("case_date_from") or "").strip()
  case_date_to = (request.args.get("case_date_to") or "").strip()
  unmatched_only_raw = request.args.get("unmatchedOnly")
  if unmatched_only_raw is None:
    unmatched_only = True
  else:
    unmatched_only = str(unmatched_only_raw).strip() in ("1", "true", "True")

  page = max(_parse_int(request.args.get("page"), 1), 1)
  per_page = max(1, min(_parse_int(request.args.get("perPage"), 15), 200))

  conn = get_db()
  _ensure_external_invoice_case_map(conn)
  is_postgres = _is_postgres(conn)
  recommend_invoice = _load_recommend_invoice_context(conn, recommend_invoice_id)
  if recommend_invoice_id > 0 and recommend_invoice is None:
    conn.close()
    return jsonify({"items": [], "total": 0, "pageNum": 1, "pageCount": 1})

  client_name_expr, overview_join_sql = _case_client_sql_parts(conn)

  where = []
  params = []

  # Priority: Link Matter View
  if linked_only:
    if invoice_id > 0:
      # Invoice Link Matter
      where.append(
        f"EXISTS (SELECT 1 FROM external_invoice_case_map e WHERE e.external_invoice_id = ? AND e.matter_id = m.matter_id AND {_not_deleted_sql('e.is_deleted')})"
      )
      params.append(int(invoice_id))
    else:
      # Invoice Link Matter
      where.append(
        f"EXISTS (SELECT 1 FROM external_invoice_case_map e WHERE e.matter_id = m.matter_id AND {_not_deleted_sql('e.is_deleted')})"
      )
  # Default: Invoice Link (Matching) Matter
  elif unmatched_only:
    where.append(
      f"NOT EXISTS (SELECT 1 FROM external_invoice_case_map e WHERE e.matter_id = m.matter_id AND {_not_deleted_sql('e.is_deleted')})"
    )

  # Matter Entry date(entered_at) Period Filters
  # entered_at NULL retained_at fallbackto 
  if case_date_from:
    if is_postgres:
      where.append("COALESCE(m.entered_at, m.retained_at)::date >= ?::date")
    else:
      where.append("date(COALESCE(m.entered_at, m.retained_at)) >= date(?)")
    params.append(case_date_from)
  if case_date_to:
    if is_postgres:
      where.append("COALESCE(m.entered_at, m.retained_at)::date <= ?::date")
    else:
      where.append("date(COALESCE(m.entered_at, m.retained_at)) <= date(?)")
    params.append(case_date_to)

  if q and not is_compact_q:
    search_clause, search_params = sql_ci_contains_any(
      ["m.our_ref", "m.right_name", client_name_expr],
      q,
    )
    if search_clause:
      where.append(search_clause)
      params += search_params

  where_sql = (" WHERE " + " AND ".join(where)) if where else ""

  base_sql = f"""
    SELECT m.matter_id,
        m.our_ref,
        m.right_name,
        {client_name_expr} AS client_name,
        m.matter_type,
        m.retained_at,
        CASE
         WHEN ? > 0 THEN
          CASE WHEN EXISTS (
           SELECT 1 FROM external_invoice_case_map e
           WHERE e.external_invoice_id = ?
            AND e.matter_id = m.matter_id
            AND {_not_deleted_sql("e.is_deleted")}
          ) THEN 1 ELSE 0 END
         ELSE 0
        END AS linked,
        (SELECT COUNT(*) FROM external_invoice_case_map e2 WHERE e2.matter_id = m.matter_id AND {_not_deleted_sql("e2.is_deleted")}) AS invoice_count,
        COALESCE(cfi.search_text, '') AS case_search_text
     FROM matter m
     LEFT JOIN case_flat_index cfi ON cfi.matter_id = m.matter_id
     {overview_join_sql}
     {where_sql}
  """

  try:
    order_by = " ORDER BY (m.retained_at IS NULL) ASC, m.retained_at DESC"
    if is_compact_q or recommend_invoice is not None:
      # -only Search:  Filters
      rows_all = conn.execute(
        base_sql + order_by,
        [int(invoice_id), int(invoice_id)] + params,
      ).fetchall()
      q_ch = to_compact(q)
      filtered = []
      for r in rows_all or []:
        txt = " ".join([str(r[1] or ""), str(r[2] or ""), str(r[3] or ""), str(r[8] or "")])
        if q_ch in to_compact(txt):
          filtered.append(r)
      if not is_compact_q:
        filtered = list(rows_all or [])
      filtered_items = []
      for r in filtered:
        item = _case_item_from_matching_row(r)
        if recommend_invoice is not None:
          recommend = score_invoice_case_recommendation(
            invoice=recommend_invoice,
            case=item,
          )
          if not recommend["recommended"]:
            continue
          item["recommended"] = True
          item["recommend_score"] = recommend["score"]
          item["recommend_reasons"] = recommend["reasons"]
        filtered_items.append(item)
      if recommend_invoice is not None:
        filtered_items.sort(
          key=lambda item: (
            int(item.get("recommend_score") or 0),
            str(item.get("retained_at") or ""),
            str(item.get("matter_id") or ""),
          ),
          reverse=True,
        )
      total = len(filtered_items)
      page, page_count = _clamp_page_for_total(page, total, per_page)
      offset = (page - 1) * per_page
      rows = []
      items = filtered_items[offset : offset + per_page]
    else:
      total = conn.execute(
        f"SELECT COUNT(*) FROM matter m LEFT JOIN case_flat_index cfi ON cfi.matter_id = m.matter_id {overview_join_sql} {where_sql}",
        params,
      ).fetchone()[0]
      page, page_count = _clamp_page_for_total(page, total, per_page)
      offset = (page - 1) * per_page
      rows = conn.execute(
        base_sql + order_by + " LIMIT ? OFFSET ?",
        [int(invoice_id), int(invoice_id)] + params + [per_page, offset],
      ).fetchall()
      items = [_case_item_from_matching_row(r) for r in rows or []]
  finally:
    conn.close()

  return jsonify(
    {"items": items, "total": int(total or 0), "pageNum": page, "pageCount": page_count}
  )


@bp.post("/link")
@role_required("admin", "staff")
def link_case_to_invoice():
  data = request.get_json(silent=True) or {}
  invoice_id = _parse_int(data.get("invoice_id"), 0)
  matter_id = (data.get("matter_id") or "").strip()

  if invoice_id <= 0 or not matter_id:
    abort(400, "invoice_id and matter_id are required")

  conn = get_db()
  _ensure_external_invoice_case_map(conn)
  try:
    ok = repo_link_case_to_invoice(conn, invoice_id=int(invoice_id), matter_id=str(matter_id))
    if not ok:
      abort(404, "case not found")
    return jsonify({"ok": True})
  finally:
    conn.close()


@bp.post("/unlink")
@role_required("admin", "staff")
def unlink_case_from_invoice():
  data = request.get_json(silent=True) or {}
  invoice_id = _parse_int(data.get("invoice_id"), 0)
  matter_id = (data.get("matter_id") or "").strip()

  if invoice_id <= 0 or not matter_id:
    abort(400, "invoice_id and matter_id are required")

  conn = get_db()
  _ensure_external_invoice_case_map(conn)
  try:
    repo_unlink_case_from_invoice(conn, invoice_id=int(invoice_id), matter_id=str(matter_id))
    return jsonify({"ok": True})
  finally:
    conn.close()
