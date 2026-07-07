from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass

from flask import abort, current_app, jsonify, redirect, request, send_from_directory, url_for
from flask_login import current_user

from app.services.billing.llm_parser import (
  parse_foreign_remittance_proof,
  parse_foreign_remittance_proof_from_image,
  summarize_document_from_image,
  summarize_document_from_text,
)
from app.services.core.llm_runtime import get_openai_api_key
from app.services.uploads.intake_security import (
  UploadSecurityError,
  scan_upload_path,
  validate_upload_path,
)
from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import is_invoice_manager
from app.utils.upload_io import UploadTooLarge, resolve_first_positive_int
from app.utils.upload_io import save_upload_stream as _save_upload_stream_impl

from ..auth import get_current_user, log_audit
from ..db import get_db
from ..services.remittance_proof_service import (
  FOREIGN_REMITTANCE_PROOF_ROLE,
  GENERAL_ATTACHMENT_ROLE,
  ensure_invoice_attachment_role_schema,
  normalize_invoice_attachment_role,
)
from .invoices import bp


class _UploadTooLarge(UploadTooLarge):
  pass


def _max_attachment_bytes() -> int:
  return resolve_first_positive_int(
    (
      "INVOICE_ATTACHMENT_MAX_BYTES",
      "FILE_ASSET_MAX_BYTES",
      "UPLOAD_MAX_BYTES",
      "MAX_CONTENT_LENGTH",
    ),
    default=0,
  )


def _save_upload_stream(file_obj, dst: str, *, max_bytes: int) -> int:
  return _save_upload_stream_impl(
    file_obj,
    dst,
    max_bytes=max_bytes,
    too_large_exc=_UploadTooLarge,
    context_prefix="billing_invoices.invoices_attachments._save_upload_stream",
    report_seek_errors=False,
    log_window_seconds=300,
  )


def _allowed_attachment_exts() -> set[str]:
  allowed = current_app.config.get("ALLOWED_ATTACHMENT_EXTENSIONS", set())
  if isinstance(allowed, str):
    values = re.split(r"[,\s]+", allowed)
  elif isinstance(allowed, (list, tuple, set)):
    values = [str(value) for value in allowed]
  else:
    values = []
  return {value.strip().lower().lstrip(".") for value in values if value.strip()}


def _allowed_attachment_exts_with_dot() -> set[str]:
  return {f".{ext}" for ext in _allowed_attachment_exts() if ext}


def _allowed_attachment(filename: str) -> bool:
  if not filename or "." not in filename:
    return False
  ext = filename.rsplit(".", 1)[1].lower()
  return ext in _allowed_attachment_exts()


def _remove_file_if_exists(path: str | None) -> None:
  if not path:
    return
  try:
    if os.path.exists(path):
      os.remove(path)
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices_attachments.remove_file_if_exists",
      log_key="billing_invoices.invoices_attachments.remove_file_if_exists",
      log_window_seconds=300,
    )


def _validate_stored_attachment(path: str, *, filename: str) -> None:
  validation = validate_upload_path(
    path,
    filename=filename,
    allowed_exts=_allowed_attachment_exts_with_dot(),
  )
  if not validation.ok:
    raise UploadSecurityError("upload_validation_failed")
  scan_upload_path(path, filename=filename)


def _attachment_dir(invoice_id: int) -> str:
  base = current_app.config.get("ATTACHMENTS_DIR")
  path = os.path.join(base, f"invoice_{invoice_id}")
  os.makedirs(path, exist_ok=True)
  return path


def _extract_pdf_first_page_text(file_path: str) -> str:
  """Extract text from the first page of a PDF. Returns empty string on failure."""
  try:
    from pypdf import PdfReader

    with open(file_path, "rb") as f:
      reader = PdfReader(f)
      if not reader.pages:
        return ""
      page = reader.pages[0]
      text = page.extract_text() or ""
      return text.strip()
  except Exception:
    return ""


def _render_pdf_first_page_image(file_path: str, dpi: int = 200):
  """Render the first page of a PDF to a PIL Image using pypdfium2. Returns None on failure."""
  try:
    import pypdfium2 as pdfium

    scale = max(dpi, 72) / 72.0
    pdf = pdfium.PdfDocument(file_path)
    if len(pdf) <= 0:
      return None
    page = pdf[0]
    bitmap = page.render(scale=scale)
    img = bitmap.to_pil()
    return img
  except Exception:
    return None


def _is_text_readable(text: str) -> bool:
  if not text:
    return False
  compact = re.sub(r"\s+", "", text)
  if len(compact) < 30:
    return False
  readable = len(re.findall(r"[^\W_]", compact, flags=re.UNICODE))
  alnum = len(re.findall(r"[A-Za-z0-9]", compact))
  return max(readable, alnum) / len(compact) > 0.3


def _sanitize_filename_preserve_unicode(name: str) -> str:
  """Sanitize a filename while preserving Unicode characters."""
  try:
    name = os.path.basename(name or "")
    name = unicodedata.normalize("NFC", name)
    name = name.replace("\x00", "")
    invalid = '<>:"/\\|?*\r\n\t'
    table = {ord(ch): "_" for ch in invalid}
    name = name.translate(table)
    name = name.strip(" .")
    base, ext = os.path.splitext(name)
    if not base:
      base = "file"
    reserved = {
      "CON",
      "PRN",
      "AUX",
      "NUL",
      "COM1",
      "COM2",
      "COM3",
      "COM4",
      "COM5",
      "COM6",
      "COM7",
      "COM8",
      "COM9",
      "LPT1",
      "LPT2",
      "LPT3",
      "LPT4",
      "LPT5",
      "LPT6",
      "LPT7",
      "LPT8",
      "LPT9",
    }
    if base.upper() in reserved:
      base = f"_{base}"
    if len(ext) > 30:
      ext = ext[:30]
    max_total = 200
    max_base = max_total - len(ext)
    if len(base) > max_base:
      base = base[:max_base]
    cleaned = base + ext
    if not cleaned:
      cleaned = "file"
    return cleaned
  except Exception:
    return "file"


def _unique_stored_name(directory: str, filename: str) -> str:
  """Return a unique filename within directory, appending ' (n)' before extension if needed."""
  filename = os.path.basename(filename)
  base, ext = os.path.splitext(filename)
  if not base:
    base = "file"
  candidate = base + ext
  i = 1
  while os.path.exists(os.path.join(directory, candidate)):
    candidate = f"{base} ({i}){ext}"
    i += 1
    if i > 500:
      candidate = f"{base}__{uuid.uuid4().hex}{ext}"
      break
  return candidate


@dataclass
class InvoiceAttachmentAnalysis:
  invoice_id: int
  original_name: str
  stored_name: str
  stored_path: str
  content_type: str | None
  size: int
  role: str
  first_page_text: str | None
  analysis_meta: dict | None


class InvoiceAttachmentAnalyzeUseCase:
  def __init__(self, *, invoice_id: int, upload_dir: str, max_bytes: int, role: str):
    self.invoice_id = invoice_id
    self.upload_dir = upload_dir
    self.max_bytes = max_bytes
    self.role = normalize_invoice_attachment_role(role)

  def execute(self, file_obj) -> InvoiceAttachmentAnalysis | None:
    if not file_obj or not getattr(file_obj, "filename", ""):
      return None

    filename = file_obj.filename
    if not _allowed_attachment(filename):
      return None

    if self.max_bytes:
      try:
        content_len = int(getattr(file_obj, "content_length", 0) or 0)
      except Exception:
        content_len = 0
      if content_len and content_len > self.max_bytes:
        raise _UploadTooLarge("File .")

    original = filename
    sanitized = _sanitize_filename_preserve_unicode(original)
    stored = _unique_stored_name(self.upload_dir, sanitized)
    dst = os.path.join(self.upload_dir, stored)
    try:
      size = _save_upload_stream(file_obj, dst, max_bytes=self.max_bytes)
      _validate_stored_attachment(dst, filename=original)
    except Exception:
      _remove_file_if_exists(dst)
      raise
    content_type = getattr(file_obj, "mimetype", None)

    first_page_text = None
    analysis_meta = None
    try:
      ext_lower = os.path.splitext(stored)[1].lower()
      is_pdf = (content_type and "pdf" in content_type.lower()) or ext_lower == ".pdf"
    except Exception:
      is_pdf = False
    try:
      is_image = bool(content_type and content_type.lower().startswith("image/"))
    except Exception:
      is_image = False

    if is_pdf:
      txt = _extract_pdf_first_page_text(dst)
      api_key = get_openai_api_key(allow_legacy=False)
      first_page_text = txt if (txt and txt.strip()) else None
      if self.role == FOREIGN_REMITTANCE_PROOF_ROLE:
        if txt and _is_text_readable(txt):
          try:
            analysis_meta = parse_foreign_remittance_proof(txt[:8000], api_key)
          except Exception:
            analysis_meta = None
        elif api_key:
          try:
            dpi = int(current_app.config.get("OCR_DPI", 200) or 200)
            img = _render_pdf_first_page_image(dst, dpi=dpi)
            if img is not None:
              analysis_meta = parse_foreign_remittance_proof_from_image(img, api_key)
              if not first_page_text:
                first_page_text = "(Scanned PDF - Processed by Vision)"
          except Exception:
            analysis_meta = None
      elif txt and _is_text_readable(txt):
        if api_key:
          try:
            limited = txt[:8000]
            analysis_meta = summarize_document_from_text(limited, api_key)
          except Exception:
            analysis_meta = None
      else:
        if api_key:
          try:
            dpi = int(current_app.config.get("OCR_DPI", 200) or 200)
            img = _render_pdf_first_page_image(dst, dpi=dpi)
            if img is not None:
              analysis_meta = summarize_document_from_image(img, api_key)
              if not first_page_text:
                first_page_text = "(Scanned PDF - Processed by Vision)"
          except Exception:
            analysis_meta = None
    elif self.role == FOREIGN_REMITTANCE_PROOF_ROLE and is_image:
      api_key = get_openai_api_key(allow_legacy=False)
      if api_key:
        try:
          from PIL import Image

          with Image.open(dst) as img:
            analysis_meta = parse_foreign_remittance_proof_from_image(img, api_key)
          first_page_text = "(Image File - Processed by Vision)"
        except Exception:
          analysis_meta = None

    if self.role == FOREIGN_REMITTANCE_PROOF_ROLE and isinstance(analysis_meta, dict):
      has_remittance_value = any(
        analysis_meta.get(key)
        for key in (
          "summary",
          "sender",
          "receiver",
          "amount",
          "currency",
          "date",
          "reference",
        )
      )
      if not has_remittance_value:
        analysis_meta = None

    return InvoiceAttachmentAnalysis(
      invoice_id=self.invoice_id,
      original_name=original,
      stored_name=stored,
      stored_path=dst,
      content_type=content_type,
      size=size,
      role=self.role,
      first_page_text=first_page_text,
      analysis_meta=analysis_meta,
    )


class InvoiceAttachmentApplyUseCase:
  def __init__(self, *, conn, user):
    self.conn = conn
    self.user = user

  def execute(self, analysis: InvoiceAttachmentAnalysis) -> dict:
    ensure_invoice_attachment_role_schema(self.conn)
    analysis_meta_str = None
    if analysis.analysis_meta is not None:
      try:
        analysis_meta_str = json.dumps(analysis.analysis_meta, ensure_ascii=False)
      except Exception:
        analysis_meta_str = None

    cur = self.conn.execute(
      """INSERT INTO invoice_attachments
        (invoice_id, original_name, stored_name, content_type, size, role, uploaded_by, first_page_text, analysis_meta)
        VALUES (?,?,?,?,?,?,?,?,?)""",
      (
        analysis.invoice_id,
        analysis.original_name,
        analysis.stored_name,
        analysis.content_type,
        analysis.size,
        analysis.role,
        self.user["id"] if self.user else None,
        analysis.first_page_text,
        analysis_meta_str,
      ),
    )
    att_id = cur.lastrowid
    saved_item = {
      "id": att_id,
      "name": analysis.original_name,
      "size": analysis.size,
      "content_type": analysis.content_type,
      "role": analysis.role,
    }
    if analysis_meta_str:
      try:
        saved_item["analysis"] = json.loads(analysis_meta_str)
      except (json.JSONDecodeError, TypeError, ValueError):
        saved_item["analysis"] = None
    return saved_item


@bp.route("/<int:invoice_id>/attachments", methods=["GET"])
def list_attachments(invoice_id):
  conn = get_db()
  ensure_invoice_attachment_role_schema(conn)
  role = normalize_invoice_attachment_role(request.args.get("role"))
  params: list[object] = [invoice_id]
  where = "WHERE invoice_id=?"
  if request.args.get("role") is not None:
    where += " AND COALESCE(role, 'general')=?"
    params.append(role)
  rows = conn.execute(
    "SELECT id, original_name, size, uploaded_at, content_type, COALESCE(role, 'general') AS role, analysis_meta "
    f"FROM invoice_attachments {where} ORDER BY uploaded_at DESC",
    params,
  ).fetchall()
  conn.close()
  items = []
  for r in rows:
    analysis = None
    try:
      if r["analysis_meta"]:
        analysis = json.loads(r["analysis_meta"]) # type: ignore
    except Exception:
      analysis = None
    items.append(
      {
        "id": r["id"],
        "name": r["original_name"],
        "size": r["size"],
        "uploaded_at": r["uploaded_at"],
        "content_type": r["content_type"],
        "role": r["role"],
        "analysis": analysis,
      }
    )
  return jsonify(items)


@bp.route("/<int:invoice_id>/attachments/upload", methods=["POST"])
def upload_attachments(invoice_id):
  conn = get_db()
  inv = conn.execute("SELECT id FROM invoices WHERE id=?", (invoice_id,)).fetchone()
  if not inv:
    conn.close()
    abort(404)

  files = request.files.getlist("files[]") or request.files.getlist("files")
  if not files:
    files = [request.files.get("file")] if request.files.get("file") else []
  if not files:
    conn.close()
    return jsonify({"success": False, "error": "File not available."}), 400

  saved = []
  user = get_current_user()
  upload_dir = _attachment_dir(invoice_id)
  max_bytes = _max_attachment_bytes()
  role = normalize_invoice_attachment_role(request.form.get("role") or GENERAL_ATTACHMENT_ROLE)
  analyzer = InvoiceAttachmentAnalyzeUseCase(
    invoice_id=invoice_id,
    upload_dir=upload_dir,
    max_bytes=max_bytes,
    role=role,
  )
  applier = InvoiceAttachmentApplyUseCase(conn=conn, user=user)
  for f in files:
    try:
      analysis = analyzer.execute(f)
    except _UploadTooLarge as exc:
      conn.close()
      return jsonify({"success": False, "error": str(exc)}), 413
    except UploadSecurityError:
      conn.close()
      return jsonify({"success": False, "error": "Upload  failed."}), 400
    except Exception:
      conn.close()
      return jsonify({"success": False, "error": "Upload failed"}), 500
    if not analysis:
      continue
    try:
      saved_item = applier.execute(analysis)
    except Exception as exc:
      try:
        if analysis.stored_path and os.path.exists(analysis.stored_path):
          os.remove(analysis.stored_path)
      except Exception as cleanup_exc:
        report_swallowed_exception(
          cleanup_exc,
          context="billing_invoices.invoices_attachments.apply.cleanup_remove",
          log_key="billing_invoices.invoices_attachments.apply.cleanup_remove",
          log_window_seconds=300,
        )
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoices_attachments.apply.save_db",
        log_key="billing_invoices.invoices_attachments.apply.save_db",
        log_window_seconds=300,
      )
      conn.close()
      return jsonify({"success": False, "error": "Upload failed"}), 500
    saved.append(saved_item)
    log_audit(
      "invoice.attachment.upload",
      "invoice",
      invoice_id,
      json.dumps(
        {
          "attachment_id": saved_item["id"],
          "name": saved_item["name"],
          "role": saved_item.get("role") or GENERAL_ATTACHMENT_ROLE,
        },
        ensure_ascii=False,
      ),
    )
  conn.commit()
  conn.close()
  return jsonify({"success": True, "attachments": saved}), 200


@bp.route("/<int:invoice_id>/attachments/<int:att_id>/download")
def download_attachment(invoice_id, att_id):
  conn = get_db()
  row = conn.execute(
    "SELECT original_name, stored_name FROM invoice_attachments WHERE id=? AND invoice_id=?",
    (att_id, invoice_id),
  ).fetchone()
  conn.close()
  if not row:
    abort(404)
  directory = _attachment_dir(invoice_id)

  sn_raw = str(row["stored_name"] or "")
  sn = os.path.basename(sn_raw)
  if not sn or sn != sn_raw:
    abort(404)

  path = os.path.join(directory, sn)
  if not os.path.exists(path):
    abort(404)

  inline_raw = (request.args.get("inline") or "").strip().lower()
  inline_requested = inline_raw in ("1", "true", "yes", "y")
  try:
    original_name = str(row["original_name"] or "")
  except Exception:
    original_name = ""
  ext = os.path.splitext(original_name or sn)[1].lower().lstrip(".")
  inline_allowed = ext in {"pdf", "png", "jpg", "jpeg", "gif", "webp"}
  inline = bool(inline_requested and inline_allowed)
  return send_from_directory(
    directory,
    sn,
    as_attachment=(not inline),
    download_name=row["original_name"],
  )


@bp.route("/<int:invoice_id>/attachments/<int:att_id>/delete", methods=["POST"])
def delete_attachment(invoice_id, att_id):
  if not is_invoice_manager(current_user):
    abort(403)
  conn = get_db()
  row = conn.execute(
    "SELECT original_name, stored_name FROM invoice_attachments WHERE id=? AND invoice_id=?",
    (att_id, invoice_id),
  ).fetchone()
  if not row:
    conn.close()
    abort(404)
  directory = _attachment_dir(invoice_id)
  sn_raw = str(row["stored_name"] or "")
  sn = os.path.basename(sn_raw)
  filepath = None
  if sn and sn == sn_raw:
    filepath = os.path.join(directory, sn)
  try:
    if filepath and os.path.exists(filepath):
      os.remove(filepath)
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices_attachments.delete_attachment.remove_file",
      log_key="billing_invoices.invoices_attachments.delete_attachment.remove_file",
      log_window_seconds=300,
    )
  conn.execute("DELETE FROM invoice_attachments WHERE id=?", (att_id,))
  conn.commit()
  conn.close()
  log_audit(
    "invoice.attachment.delete",
    "invoice",
    invoice_id,
    json.dumps(
      {"attachment_id": att_id, "name": row["original_name"]},
      ensure_ascii=False,
    ),
  )
  if request.is_json:
    return jsonify({"success": True})
  return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/attachments/<int:att_id>/meta", methods=["POST"])
def update_attachment_meta(invoice_id, att_id):
  """Update user-provided memo for an attachment."""
  conn = get_db()
  row = conn.execute(
    "SELECT id, analysis_meta FROM invoice_attachments WHERE id=? AND invoice_id=?",
    (att_id, invoice_id),
  ).fetchone()
  if not row:
    conn.close()
    abort(404)

  memo = (request.form.get("memo") or "").strip()
  if not memo:
    payload = (request.json or {}) if request.is_json else {}
    memo = payload.get("memo") or payload.get("user_memo") or ""
    memo = str(memo or "").strip()

  existing = row.get("analysis_meta") if isinstance(row, dict) else row["analysis_meta"]
  if not existing:
    existing = None
  try:
    meta = json.loads(existing) if existing else {}
    if not isinstance(meta, dict):
      meta = {}
  except Exception:
    meta = {}
  meta["user_memo"] = memo
  meta_str = json.dumps(meta, ensure_ascii=False)

  conn.execute(
    "UPDATE invoice_attachments SET analysis_meta=? WHERE id=? AND invoice_id=?",
    (meta_str, att_id, invoice_id),
  )
  conn.commit()
  conn.close()

  try:
    log_audit(
      "invoice.attachment.meta",
      "invoice",
      invoice_id,
      json.dumps({"attachment_id": att_id, "memo_len": len(memo)}, ensure_ascii=False),
    )
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices_attachments.update_attachment_meta.log_audit",
      log_key="billing_invoices.invoices_attachments.update_attachment_meta.log_audit",
      log_window_seconds=300,
    )

  return jsonify({"success": True, "analysis": meta})
