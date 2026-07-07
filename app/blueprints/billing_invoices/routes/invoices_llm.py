from __future__ import annotations

import json

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, url_for

from app.services.billing.llm_parser import parse_customer_from_text
from app.services.client.name_normalization import normalize_client_name
from app.services.core.llm_runtime import get_llm_input_max_chars, get_openai_api_key

from ..auth import log_audit
from ..db import get_db, row_get
from .invoices import bp


def _clamp_llm_text(text: str) -> tuple[str, bool]:
  limit = get_llm_input_max_chars()
  if limit and len(text) > limit:
    return text[:limit], True
  return text, False


@bp.route("/check_duplicate_customer", methods=["POST"])
def check_duplicate_customer():
  """Client Duplicate Confirm AJAX - Client """
  try:
    if not request.is_json:
      return jsonify({"success": False, "error": "Invalid request"}), 400

    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    phone = data.get("phone", "").strip()
    email = data.get("email", "").strip()
    address = data.get("address", "").strip()

    if not name:
      return jsonify({"success": True, "matches": []}), 200

    conn = get_db()

    potential_matches = conn.execute(
      "SELECT id, name, phone, email, address, manager FROM clients "
      "WHERE name=? AND is_deleted IS NOT TRUE",
      (name,),
    ).fetchall()

    matches = []
    for client in potential_matches:
      score = 0
      reasons = ["Client name match"]

      if phone and client["phone"]:
        clean_phone = phone.replace("-", "").replace(" ", "")
        clean_client_phone = client["phone"].replace("-", "").replace(" ", "")
        if clean_phone == clean_client_phone:
          score += 90
          reasons.append("Phone match")

      if email and client["email"]:
        if email.lower() == client["email"].lower():
          score += 90
          reasons.append("Email match")

      if address and client["address"]:
        if address in client["address"] or client["address"] in address:
          score += 50
          reasons.append("Address ")

      matches.append(
        {
          "id": client["id"],
          "name": client["name"],
          "phone": client["phone"],
          "email": client["email"],
          "address": client["address"],
          "manager": client["manager"],
          "score": score,
          "reasons": reasons,
        }
      )

    matches.sort(key=lambda x: x["score"], reverse=True)

    conn.close()
    return jsonify({"success": True, "matches": matches}), 200

  except Exception as e:
    current_app.logger.exception("Customer duplication check error")
    return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/parse_customer_llm", methods=["POST"])
def parse_customer_llm():
  """Email from LLM Client information AJAX """
  try:
    if not request.is_json:
      return (
        jsonify({"success": False, "error": "Content-Type must be application/json"}),
        400,
      )

    data = request.get_json(silent=True) or {}
    email_text = (data.get("email_text") or "").strip()

    if not email_text:
      return jsonify({"success": False, "error": " enter."}), 400

    email_text, truncated = _clamp_llm_text(email_text)
    api_key = get_openai_api_key(allow_legacy=False)
    if not api_key:
      return (
        jsonify(
          {
            "success": False,
            "error": "OpenAI API  .  OPENAI_API_KEY confirm.",
          }
        ),
        500,
      )

    customer_data = parse_customer_from_text(email_text, api_key)
    if not isinstance(customer_data, dict):
      customer_data = {}
    customer_data.update(normalize_client_name(customer_data.get("name"), api_key=api_key))
    return (
      jsonify({"success": True, "customer": customer_data, "truncated": truncated}),
      200,
    )

  except Exception as e:
    current_app.logger.exception("LLM parsing error")
    return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/<int:invoice_id>/parse_payment_llm", methods=["POST"])
def parse_payment_llm(invoice_id):
  """Parse payment text via OpenAI and return structured fields."""
  try:
    if not request.is_json:
      return (
        jsonify({"success": False, "error": "Content-Type must be application/json"}),
        400,
      )
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
      return jsonify({"success": False, "error": " enter."}), 400
    text, truncated = _clamp_llm_text(text)

    from app.services.billing.llm_parser import parse_payment_from_text

    invoice_currency = (request.args.get("currency") or "USD").upper()

    api_key = get_openai_api_key(allow_legacy=False)
    result, parser_type = parse_payment_from_text(text, invoice_currency, api_key)

    parser_label = "🔧  " if parser_type == "rule" else "🤖 AI(LLM) "
    return (
      jsonify(
        {
          "success": True,
          "payment_meta": result,
          "parser_type": parser_type,
          "parser_label": parser_label,
          "truncated": truncated,
        }
      ),
      200,
    )

  except Exception as e:
    current_app.logger.exception("LLM payment parsing error")
    return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/<int:invoice_id>/save_as_template", methods=["GET", "POST"])
def save_as_template(invoice_id):
  """Invoice Templateto Save"""
  conn = get_db()

  invoice = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()

  if not invoice:
    conn.close()
    abort(404, "Invoice not found.")

  items = conn.execute("SELECT * FROM line_items WHERE invoice_id=?", (invoice_id,)).fetchall()

  if request.method == "POST":
    template_name = request.form.get("template_name", "").strip()

    if not template_name:
      flash("Template name enter.", "error")
      conn.close()
      return redirect(
        url_for("billing_invoices.invoices.save_as_template", invoice_id=invoice_id)
      )

    existing = conn.execute(
      "SELECT id FROM invoice_templates WHERE name=?", (template_name,)
    ).fetchone()

    if existing:
      flash(
        f"❌ Template name '{template_name}'() exists. Name .",
        "error",
      )
      conn.close()
      return render_template(
        "save_template_form.html",
        invoice=invoice,
        items=items,
        item_count=len(items),
        submitted_name=template_name,
      )

    default_items = []

    item_indices = set()
    for key in request.form.keys():
      if key.startswith("items["):
        idx = int(key.split("[")[1].split("]")[0])
        item_indices.add(idx)

    for idx in sorted(item_indices):
      desc = request.form.get(f"items[{idx}][description]", "").strip()
      if not desc:
        continue

      item_type = request.form.get(f"items[{idx}][item_type]", "service")
      qty = float(request.form.get(f"items[{idx}][qty]", 1))
      unit_price = float(request.form.get(f"items[{idx}][unit_price]", 0))
      discount = float(request.form.get(f"items[{idx}][discount]", 0))
      phase = request.form.get(f"items[{idx}][phase]", "app")
      is_estimated_val = request.form.get(f"items[{idx}][is_estimated]", "0")
      is_estimated = 1 if is_estimated_val == "1" else 0

      fx_currency = request.form.get(f"items[{idx}][fx_currency]", "")
      fx_fee = request.form.get(f"items[{idx}][fx_fee]", "0")
      fx_gov = request.form.get(f"items[{idx}][fx_gov]", "0")
      fx_markup = request.form.get(f"items[{idx}][fx_markup]", "3")
      fx_rate_used = request.form.get(f"items[{idx}][fx_rate_used]", "")

      item_data = {
        "description": desc,
        "qty": qty,
        "unit_price": unit_price,
        "item_type": item_type,
        "discount": discount,
        "is_taxable": 1 if item_type == "service" else 0,
        "phase": phase,
        "is_estimated": is_estimated,
      }

      if item_type == "foreign":
        item_data["fx_currency"] = fx_currency.upper() if fx_currency else None
        item_data["fx_fee"] = float(fx_fee) if fx_fee else 0
        item_data["fx_gov"] = float(fx_gov) if fx_gov else 0
        item_data["fx_markup"] = float(fx_markup) if fx_markup else 3
        item_data["fx_rate_used"] = float(fx_rate_used) if fx_rate_used else None

      default_items.append(item_data)

    if not default_items:
      flash("At least one item is required.", "error")
      conn.close()
      return render_template(
        "save_template_form.html",
        invoice=invoice,
        items=items,
        item_count=len(items),
        submitted_name=template_name,
      )

    cur = conn.cursor()

    try:
      invoice_language = row_get(invoice, "language", default="en")
    except Exception:
      invoice_language = "en"

    cur.execute(
      "INSERT INTO invoice_templates (name, description, default_items, business_profile_id, language) "
      "VALUES (?, ?, ?, ?, ?)",
      (
        template_name,
        f"Invoice {invoice['number']}from ",
        json.dumps(default_items, ensure_ascii=False),
        invoice["business_profile_id"],
        invoice_language,
      ),
    )
    template_id = cur.lastrowid

    conn.commit()
    conn.close()

    log_audit(
      "template.create_from_invoice",
      "template",
      template_id,
      f'{{"template_name": "{template_name}", "source_invoice_id": {invoice_id}, '
      f'"item_count": {len(items)}}}',
    )

    flash(f"✅ Template '{template_name}'() .", "success")
    return redirect(url_for("billing_invoices.templates_bp.list_templates"))

  conn.close()
  return render_template(
    "save_template_form.html", invoice=invoice, items=items, item_count=len(items)
  )
