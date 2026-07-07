import json

from flask import Blueprint, Response, abort, redirect, render_template, request, url_for

from app.services.billing.utils import is_compact_query, sql_ci_contains_any, to_compact

from ..auth import log_audit
from ..db import get_all_business_profiles, get_db, row_to_dict

bp = Blueprint("templates_bp", __name__)


@bp.route("")
def list_templates():
  """
  Template List + Search/Filters/Pagination
  """
  q = (request.args.get("q") or "").strip()
  is_compact_q = q and is_compact_query(q)
  bp_id = (request.args.get("business_profile_id") or "").strip()
  lang = (request.args.get("language") or "").strip()
  sort = (request.args.get("sort") or "name").strip()
  try:
    page = int(request.args.get("page", 1) or 1)
  except Exception:
    page = 1
  page = max(page, 1)
  try:
    per_page = int(request.args.get("per_page", 25) or 25)
  except Exception:
    per_page = 25
  per_page = min(max(per_page, 10), 200)

  where = []
  params = []
  # days Searchdays SQL LIKE , -only Search Process row
  if q and not is_compact_q:
    search_clause, search_params = sql_ci_contains_any(["name", "description"], q)
    if search_clause:
      where.append(search_clause)
      params += search_params
  if bp_id:
    if bp_id.isdigit():
      where.append("(business_profile_id = ?)")
      params.append(int(bp_id))
  if lang:
    where.append("(language = ?)")
    params.append(lang)
  where_sql = (" WHERE " + " AND ".join(where)) if where else ""

  if sort == "recent":
    order_clause = "id DESC"
  elif sort == "items":
    # Sort by default item count, then by name for stable UI display.
    order_clause = "name COLLATE NOCASE"
  elif sort == "payment_terms":
    order_clause = "payment_terms DESC, name COLLATE NOCASE"
  else:
    order_clause = "name COLLATE NOCASE"

  conn = get_db()

  base_sql = f"SELECT * FROM invoice_templates{where_sql} ORDER BY {order_clause}"

  if is_compact_q:
    # -only Search: SQLfrom q Filters  times from  Filters
    rows_all = conn.execute(base_sql, params).fetchall()
    q_compact = to_compact(q)
    filtered = []
    for r in rows_all:
      d = row_to_dict(r)
      name = str(d.get("name") or "")
      desc = str(d.get("description") or "")
      text = " ".join([name, desc])
      if q_compact in to_compact(text):
        filtered.append(r)
    total_count = len(filtered)
    total_pages = (total_count + per_page - 1) // per_page if total_count else 1
    if page > total_pages:
      page = total_pages
    offset = (page - 1) * per_page
    rows = filtered[offset : offset + per_page]
  else:
    # Existing: SQL COUNT + LIMIT/OFFSET Pagination
    total_count = conn.execute(
      f"SELECT COUNT(*) FROM invoice_templates{where_sql}", params
    ).fetchone()[0]
    total_pages = (total_count + per_page - 1) // per_page if total_count else 1
    if page > total_pages:
      page = total_pages
    offset = (page - 1) * per_page
    rows = conn.execute(
      base_sql + " LIMIT ? OFFSET ?",
      params + [per_page, offset],
    ).fetchall()

  conn.close()

  all_profiles = get_all_business_profiles()

  return render_template(
    "templates_list.html",
    templates=rows,
    q=q,
    bp_id=bp_id,
    lang=lang,
    sort=sort,
    page=page,
    per_page=per_page,
    total_count=total_count,
    total_pages=total_pages,
    all_profiles=all_profiles,
  )


@bp.route("/new", methods=["GET", "POST"])
def new_template():
  if request.method == "POST":
    conn = get_db()
    descriptions = request.form.getlist("description[]")
    qtys = request.form.getlist("qty[]")
    prices = request.form.getlist("unit_price[]")
    item_types = request.form.getlist("item_type[]")
    discounts = request.form.getlist("discount[]")
    phases = request.form.getlist("phase[]")
    is_estimateds = request.form.getlist("is_estimated_base[]")
    fx_curs = request.form.getlist("fx_currency[]")
    fx_fees = request.form.getlist("fx_fee[]")
    fx_govs = request.form.getlist("fx_gov[]")
    fx_markups = request.form.getlist("fx_markup[]")
    fx_rates_used = request.form.getlist("fx_rate_used[]")
    foreign_vat_bases = request.form.getlist("foreign_vat_base[]")
    items = []
    for i, desc in enumerate(descriptions):
      if (desc or "").strip():
        item_type = item_types[i] if i < len(item_types) else "service"
        phase_val = phases[i].strip() if i < len(phases) and phases[i] else "app"
        is_estimated = 1 if (i < len(is_estimateds) and is_estimateds[i] == "1") else 0
        fx_currency = (
          fx_curs[i].upper().strip() if i < len(fx_curs) and fx_curs[i] else None
        )
        fx_fee = fx_fees[i] if i < len(fx_fees) and fx_fees[i] else "0"
        fx_gov = fx_govs[i] if i < len(fx_govs) and fx_govs[i] else "0"
        fx_rate_used = (
          fx_rates_used[i] if i < len(fx_rates_used) and fx_rates_used[i] else None
        )
        # foreign Sales tax flag
        fv = 0
        try:
          if item_type == "foreign" and i < len(foreign_vat_bases):
            fv = 1 if (foreign_vat_bases[i] == "1") else 0
            fx_markup = "2" if not fx_markups or i >= len(fx_markups) else fx_markups[i]
        except Exception:
          fv = 0
        items.append(
          {
            "description": (desc or "").strip(),
            "qty": float(qtys[i]) if i < len(qtys) else 1,
            "unit_price": float(prices[i]) if i < len(prices) else 0,
            "item_type": item_type,
            "discount": float(discounts[i]) if i < len(discounts) else 0,
            "phase": phase_val,
            "is_estimated": is_estimated,
            "fx_currency": (fx_currency if item_type == "foreign" else None),
            "fx_fee": (float(fx_fee) if item_type == "foreign" else None),
            "fx_gov": (float(fx_gov) if item_type == "foreign" else None),
            "fx_markup": (float(fx_markup) if item_type == "foreign" else None),
            "fx_rate_used": (
              float(fx_rate_used) if item_type == "foreign" and fx_rate_used else None
            ),
            "is_taxable": (
              1
              if item_type == "service"
              else (1 if (item_type == "foreign" and fv == 1) else 0)
            ),
          }
        )

    # Get optional business_profile_id and language
    business_profile_id = request.form.get("business_profile_id")
    business_profile_id = int(business_profile_id) if business_profile_id else None
    language = request.form.get("language") or None

    try:
      conn.execute(
        "INSERT INTO invoice_templates (name, description, default_items, payment_terms, notes, business_profile_id, language) VALUES (?,?,?,?,?,?,?)",
        (
          request.form["name"],
          request.form.get("description"),
          json.dumps(items),
          int(request.form.get("payment_terms") or 30),
          request.form.get("notes"),
          business_profile_id,
          language,
        ),
      )
      conn.commit()
      conn.close()
      log_audit(
        "template.create",
        "template",
        None,
        f'{{"name":"{request.form.get("name","")}"}}',
      )
      return redirect(url_for("billing_invoices.templates_bp.list_templates"))
    except Exception:
      conn.close()
      abort(400, " Template name.")

  all_profiles = get_all_business_profiles()
  return render_template("template_form.html", template=None, all_profiles=all_profiles)


@bp.route("/<int:template_id>/edit", methods=["GET", "POST"])
def edit_template(template_id):
  conn = get_db()
  template = conn.execute("SELECT * FROM invoice_templates WHERE id=?", (template_id,)).fetchone()
  if not template:
    conn.close()
    abort(404)
  if request.method == "POST":
    descriptions = request.form.getlist("description[]")
    qtys = request.form.getlist("qty[]")
    prices = request.form.getlist("unit_price[]")
    item_types = request.form.getlist("item_type[]")
    discounts = request.form.getlist("discount[]")
    phases = request.form.getlist("phase[]")
    is_estimateds = request.form.getlist("is_estimated_base[]")
    fx_curs = request.form.getlist("fx_currency[]")
    fx_fees = request.form.getlist("fx_fee[]")
    fx_govs = request.form.getlist("fx_gov[]")
    fx_markups = request.form.getlist("fx_markup[]")
    fx_rates_used = request.form.getlist("fx_rate_used[]")
    foreign_vat_bases = request.form.getlist("foreign_vat_base[]")
    items = []
    for i, desc in enumerate(descriptions):
      if (desc or "").strip():
        item_type = item_types[i] if i < len(item_types) else "service"
        phase_val = phases[i].strip() if i < len(phases) and phases[i] else "app"
        is_estimated = 1 if (i < len(is_estimateds) and is_estimateds[i] == "1") else 0
        fx_currency = (
          fx_curs[i].upper().strip() if i < len(fx_curs) and fx_curs[i] else None
        )
        fx_fee = fx_fees[i] if i < len(fx_fees) and fx_fees[i] else "0"
        fx_gov = fx_govs[i] if i < len(fx_govs) and fx_govs[i] else "0"
        fx_markup = fx_markups[i] if i < len(fx_markups) and fx_markups[i] else "2"
        fx_rate_used = (
          fx_rates_used[i] if i < len(fx_rates_used) and fx_rates_used[i] else None
        )
        # foreign Sales tax flag
        fv = 0
        try:
          if item_type == "foreign" and i < len(foreign_vat_bases):
            fv = 1 if (foreign_vat_bases[i] == "1") else 0
        except Exception:
          fv = 0
        items.append(
          {
            "description": (desc or "").strip(),
            "qty": float(qtys[i]) if i < len(qtys) else 1,
            "unit_price": float(prices[i]) if i < len(prices) else 0,
            "item_type": item_type,
            "discount": float(discounts[i]) if i < len(discounts) else 0,
            "phase": phase_val,
            "is_estimated": is_estimated,
            "fx_currency": (fx_currency if item_type == "foreign" else None),
            "fx_fee": (float(fx_fee) if item_type == "foreign" else None),
            "fx_gov": (float(fx_gov) if item_type == "foreign" else None),
            "fx_markup": (float(fx_markup) if item_type == "foreign" else None),
            "fx_rate_used": (
              float(fx_rate_used) if item_type == "foreign" and fx_rate_used else None
            ),
            "is_taxable": (
              1
              if item_type == "service"
              else (1 if (item_type == "foreign" and fv == 1) else 0)
            ),
          }
        )
    # Get optional business_profile_id and language
    business_profile_id = request.form.get("business_profile_id")
    business_profile_id = int(business_profile_id) if business_profile_id else None
    language = request.form.get("language") or None

    try:
      conn.execute(
        "UPDATE invoice_templates SET name=?, description=?, default_items=?, payment_terms=?, notes=?, business_profile_id=?, language=? WHERE id=?",
        (
          request.form["name"],
          request.form.get("description"),
          json.dumps(items),
          int(request.form.get("payment_terms") or 30),
          request.form.get("notes"),
          business_profile_id,
          language,
          template_id,
        ),
      )
      conn.commit()
      conn.close()
      log_audit(
        "template.update",
        "template",
        template_id,
        f'{{"name":"{request.form.get("name","")}"}}',
      )
      return redirect(url_for("billing_invoices.templates_bp.list_templates"))
    except Exception:
      conn.close()
      abort(400, " Template name.")

  all_profiles = get_all_business_profiles()
  conn.close()
  return render_template("template_form.html", template=template, all_profiles=all_profiles)


@bp.route("/<int:template_id>/copy", methods=["POST"])
def copy_template(template_id):
  conn = get_db()
  template = conn.execute("SELECT * FROM invoice_templates WHERE id=?", (template_id,)).fetchone()
  if not template:
    conn.close()
    abort(404)

  # Name (Duplicate )
  new_name = template["name"] + " ()"
  counter = 1
  while conn.execute("SELECT id FROM invoice_templates WHERE name=?", (new_name,)).fetchone():
    new_name = template["name"] + f" ( {counter})"
    counter += 1

  try:
    conn.execute(
      "INSERT INTO invoice_templates (name, description, default_items, payment_terms, notes, business_profile_id, language) VALUES (?,?,?,?,?,?,?)",
      (
        new_name,
        template["description"],
        template["default_items"],
        template["payment_terms"],
        template["notes"],
        template["business_profile_id"],
        template["language"],
      ),
    )
    conn.commit()
    conn.close()
    log_audit("template.copy", "template", template_id, f'{{"new_name":"{new_name}"}}')
    return redirect(url_for("billing_invoices.templates_bp.list_templates"))
  except Exception:
    conn.close()
    abort(400, "Template ")


@bp.route("/<int:template_id>/delete", methods=["POST"])
def delete_template(template_id):
  conn = get_db()
  conn.execute("DELETE FROM invoice_templates WHERE id=?", (template_id,))
  conn.commit()
  conn.close()
  log_audit("template.delete", "template", template_id)
  return redirect(url_for("billing_invoices.templates_bp.list_templates"))


@bp.route("/<int:template_id>/export", methods=["GET"])
def export_template(template_id):
  """days Template JSON Download"""
  conn = get_db()
  row = conn.execute("SELECT * FROM invoice_templates WHERE id=?", (template_id,)).fetchone()
  conn.close()
  if not row:
    abort(404)
  data = {
    "name": row["name"],
    "description": row["description"],
    "default_items": (json.loads(row["default_items"]) if row["default_items"] else []),
    "payment_terms": row["payment_terms"],
    "notes": row["notes"],
    "business_profile_id": row["business_profile_id"],
    "language": row["language"],
  }
  payload = json.dumps(data, ensure_ascii=False, indent=2)
  log_audit("template.export", "template", template_id, f'{{"name":"{row["name"]}"}}')
  return Response(
    payload,
    mimetype="application/json; charset=utf-8",
    headers={"Content-Disposition": f'attachment; filename="template_{template_id}.json"'},
  )


@bp.route("/export_all", methods=["GET"])
def export_all_templates():
  """ Template JSON column """
  conn = get_db()
  rows = conn.execute("SELECT * FROM invoice_templates ORDER BY name").fetchall()
  conn.close()
  data = []
  for row in rows:
    data.append(
      {
        "name": row["name"],
        "description": row["description"],
        "default_items": (json.loads(row["default_items"]) if row["default_items"] else []),
        "payment_terms": row["payment_terms"],
        "notes": row["notes"],
        "business_profile_id": row["business_profile_id"],
        "language": row["language"],
      }
    )
  payload = json.dumps(data, ensure_ascii=False, indent=2)
  log_audit("template.export_all", "template", None, f'{{"count": {len(data)}}}')
  return Response(
    payload,
    mimetype="application/json; charset=utf-8",
    headers={"Content-Disposition": 'attachment; filename="templates_export.json"'},
  )


@bp.route("/import", methods=["POST"])
def import_templates():
  """JSON File  Template """
  # Input 
  file = request.files.get("file")
  json_text = request.form.get("json_text")
  if not file and not json_text:
    abort(400, "File JSON required.")

  try:
    content = json_text if json_text else file.read().decode("utf-8")
    data = json.loads(content)
    if isinstance(data, dict):
      data = [data]
    assert isinstance(data, list)
  except Exception:
    abort(400, " JSON .")

  conn = get_db()
  created = 0
  for tpl in data:
    name = str(tpl.get("name", "")).strip()
    if not name:
      continue
    description = tpl.get("description")
    default_items = tpl.get("default_items") or []
    # Item 
    norm_items = []
    for it in default_items:
      try:
        norm_items.append(
          {
            "description": str(it.get("description", "")).strip(),
            "qty": float(it.get("qty", 1) or 1),
            "unit_price": float(it.get("unit_price", 0) or 0),
            "item_type": (
              it.get("item_type", "service")
              if it.get("item_type") in ("service", "admin")
              else "service"
            ),
            "discount": float(it.get("discount", 0) or 0),
          }
        )
      except Exception:
        continue
    payment_terms = int(tpl.get("payment_terms") or 30)
    notes = tpl.get("notes")
    business_profile_id = tpl.get("business_profile_id")
    business_profile_id = int(business_profile_id) if business_profile_id else None
    language = tpl.get("language") or None

    # Name  (import) (import N) Add
    new_name = name
    counter = 1
    while conn.execute("SELECT id FROM invoice_templates WHERE name=?", (new_name,)).fetchone():
      suffix = f" (import {counter})" if counter > 1 else " (import)"
      new_name = f"{name}{suffix}"
      counter += 1

    conn.execute(
      "INSERT INTO invoice_templates (name, description, default_items, payment_terms, notes, business_profile_id, language) VALUES (?,?,?,?,?,?,?)",
      (
        new_name,
        description,
        json.dumps(norm_items),
        payment_terms,
        notes,
        business_profile_id,
        language,
      ),
    )
    created += 1

  conn.commit()
  conn.close()
  log_audit("template.import", "template", None, f'{{"count": {created}}}')
  return redirect(url_for("billing_invoices.templates_bp.list_templates"))
