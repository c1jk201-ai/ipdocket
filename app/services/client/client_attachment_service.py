from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Dict, Optional

from flask import current_app
from sqlalchemy import column, delete, desc, insert, select, table
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm.attributes import flag_modified

from app.extensions import db
from app.services.billing.db_core import (
    _actual_table_name,
    safe_json_parse,
    unified_clients_enabled,
)
from app.services.core.llm_runtime import get_openai_api_key
from app.services.storage.backends import LocalStorageBackend, get_storage_backend
from app.services.storage.file_asset_service import FileAssetService, UploadTooLargeError
from app.services.uploads.intake_security import (
    UploadSecurityError,
    scan_upload_path,
    validate_upload_path,
)
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text

ALLOWED_BIZREG_EXTS = {"pdf", "png", "jpg", "jpeg", "gif"}
_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_table_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name or not _TABLE_NAME_RE.match(name):
        raise ValueError(f"Unsafe table name: {raw!r}")
    return name


def _clients_table(name: str):  # noqa: ANN001
    # Lightweight SQLAlchemy Core table for safe dynamic table names.
    return table(name, column("id"), column("ipm_client_id"))


def _client_attachments_table(name: str):  # noqa: ANN001
    # Lightweight SQLAlchemy Core table for safe dynamic table names.
    return table(
        name,
        column("id"),
        column("client_id"),
        column("original_name"),
        column("stored_name"),
        column("content_type"),
        column("size"),
        column("uploaded_at"),
        column("analysis_meta"),
        column("uploaded_by"),
    )


def _safe_stored_basename(raw: Any) -> Optional[str]:
    name = str(raw or "").strip()
    if not name:
        return None
    base = os.path.basename(name)
    if base != name:
        return None
    return base


def _attachment_base_dirs() -> list[str]:
    bases: list[str] = []
    configured = current_app.config.get("CLIENT_ATTACHMENTS_DIR", "uploads/clients")
    if configured:
        bases.append(os.path.abspath(configured))

    upload_root = current_app.config.get("UPLOAD_FOLDER")
    if upload_root:
        upload_root_abs = os.path.abspath(upload_root)
        if upload_root_abs not in bases:
            bases.append(upload_root_abs)
        # Some legacy paths ended up under UPLOAD_FOLDER/uploads/clients
        legacy_under_upload = os.path.abspath(os.path.join(upload_root_abs, "uploads", "clients"))
        if legacy_under_upload not in bases:
            bases.append(legacy_under_upload)

    try:
        project_root = Path(current_app.root_path).resolve().parent
        legacy = os.path.abspath(os.path.join(project_root, "uploads", "clients"))
        if legacy not in bases:
            bases.append(legacy)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="client_attachment_service._attachment_base_dirs.project_root",
            log_key="client_attachment_service._attachment_base_dirs.project_root",
            log_window_seconds=300,
        )

    rel = os.path.abspath("uploads/clients")
    if rel not in bases:
        bases.append(rel)

    return bases


def _candidate_attachment_dirs(
    bases: list[str],
    attachment_client_id: int,
    crm_client_id: Optional[int] = None,
    *,
    include_legacy_crm: bool = False,
) -> list[str]:
    dirs: list[str] = []
    for base in bases:
        dirs.append(os.path.join(base, f"client_{int(attachment_client_id)}"))
        if include_legacy_crm and crm_client_id is not None:
            dirs.append(os.path.join(base, f"crm_client_{int(crm_client_id)}"))

    seen: set[str] = set()
    uniq: list[str] = []
    for d in dirs:
        if d in seen:
            continue
        seen.add(d)
        uniq.append(d)
    return uniq


def resolve_client_attachment_file_path(
    attachment_client_id: int,
    stored_name: str,
    *,
    crm_client_id: Optional[int] = None,
    include_legacy_crm: bool = False,
    repair: bool = True,
) -> Optional[str]:
    stored = _safe_stored_basename(stored_name)
    if not stored:
        return None

    bases = _attachment_base_dirs()
    if not bases:
        return None

    primary_dir = os.path.join(bases[0], f"client_{int(attachment_client_id)}")
    primary_path = os.path.join(primary_dir, stored)
    if os.path.exists(primary_path):
        return os.path.abspath(primary_path)

    candidates = _candidate_attachment_dirs(
        bases,
        attachment_client_id,
        crm_client_id,
        include_legacy_crm=include_legacy_crm,
    )
    for d in candidates:
        path = os.path.join(d, stored)
        if not os.path.exists(path):
            continue
        if not repair:
            return os.path.abspath(path)

        try:
            os.makedirs(primary_dir, exist_ok=True)
            if not os.path.exists(primary_path):
                try:
                    shutil.move(path, primary_path)
                except Exception:
                    shutil.copy2(path, primary_path)
            if os.path.exists(primary_path):
                return os.path.abspath(primary_path)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="client_attachment_service.find_client_attachment_path.repair",
                log_key="client_attachment_service.find_client_attachment_path.repair",
                log_window_seconds=300,
            )
            return os.path.abspath(path)

        return os.path.abspath(path)

    return None


def open_client_attachment_stream(
    attachment_client_id: int,
    stored_name: str,
    *,
    crm_client_id: Optional[int] = None,
    include_legacy_crm: bool = False,
) -> tuple[Optional[BinaryIO], Optional[str]]:
    stored = _safe_stored_basename(stored_name)
    if not stored:
        return None, None

    try:
        backend = get_storage_backend()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="client_attachment_service.open_client_attachment_stream.get_storage_backend",
            log_key="client_attachment_service.open_client_attachment_stream.get_storage_backend",
            log_window_seconds=300,
        )
        return None, None

    if isinstance(backend, LocalStorageBackend):
        return None, None

    keys = [f"client_{int(attachment_client_id)}/{stored}"]
    if include_legacy_crm and crm_client_id is not None:
        keys.append(f"crm_client_{int(crm_client_id)}/{stored}")

    for key in keys:
        exists = False
        try:
            exists = bool(backend.exists(key))
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="client_attachment_service.open_client_attachment_stream.exists",
                log_key="client_attachment_service.open_client_attachment_stream.exists",
                log_window_seconds=300,
            )
            exists = False
        if exists:
            return backend.open(key), key

    return None, None


def _allowed_attachment_exts() -> set[str]:
    allowed = current_app.config.get("ALLOWED_ATTACHMENT_EXTENSIONS")
    if isinstance(allowed, str):
        return {e.strip().lower().lstrip(".") for e in allowed.split(",") if e.strip()}
    if isinstance(allowed, (list, tuple, set)):
        return {str(e).strip().lower().lstrip(".") for e in allowed if str(e).strip()}
    return set()


def is_allowed_attachment(filename: str) -> bool:
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    allowed = _allowed_attachment_exts()
    return ext in allowed if allowed else False


def _exts_with_dot(exts: set[str]) -> set[str]:
    return {f".{ext.strip().lower().lstrip('.')}" for ext in exts if ext and ext.strip()}


def _validate_saved_attachment_path(path: str, *, filename: str, allowed_exts: set[str]) -> None:
    validation = validate_upload_path(
        path,
        filename=filename,
        allowed_exts=_exts_with_dot(allowed_exts),
    )
    if not validation.ok:
        raise UploadSecurityError("upload_validation_failed")
    scan_upload_path(path, filename=filename)


def max_attachment_bytes() -> int:
    for key in (
        "INVOICE_ATTACHMENT_MAX_BYTES",
        "FILE_ASSET_MAX_BYTES",
        "UPLOAD_MAX_BYTES",
        "MAX_CONTENT_LENGTH",
    ):
        try:
            raw = current_app.config.get(key)
        except Exception:
            raw = None
        if raw in (None, ""):
            continue
        try:
            value = int(raw)
        except Exception:
            value = 0
        if value > 0:
            return value
    return 0


def _attachments_root() -> str:
    return current_app.config.get("CLIENT_ATTACHMENTS_DIR", "uploads/clients")


def client_attachment_dir(client_id: int) -> str:
    """Invoice   : uploads/clients/client_{id}"""
    base = _attachments_root()
    path = os.path.join(base, f"client_{int(client_id)}")
    os.makedirs(path, exist_ok=True)
    return path


def legacy_crm_bizreg_dir(crm_client_id: int) -> str:
    """Legacy CRM : uploads/clients/crm_client_{id}"""
    base = _attachments_root()
    path = os.path.join(base, f"crm_client_{int(crm_client_id)}")
    os.makedirs(path, exist_ok=True)
    return path


def _sanitize_filename_preserve_unicode(name: str) -> str:
    """File name   ( Retention) - invoice   """
    try:
        name = os.path.basename(name or "")
        name = unicodedata.normalize("NFC", name).replace("\x00", "")
        invalid = '<>:"/\\|?*\r\n\t'
        table = {ord(ch): "_" for ch in invalid}
        name = name.translate(table).strip(" .")

        base, ext = os.path.splitext(name)
        if not base:
            base = "file"

        if len(ext) > 30:
            ext = ext[:30]
        max_base = 200 - len(ext)
        if len(base) > max_base:
            base = base[:max_base]

        return f"{base}{ext}"
    except Exception:
        return "file.bin"


def _unique_stored_name(directory: str, filename: str) -> str:
    """Duplicate  File name Create - invoice   """
    base, ext = os.path.splitext(os.path.basename(filename))
    if not base:
        base = "file"
    candidate = f"{base}{ext}"
    i = 1
    while os.path.exists(os.path.join(directory, candidate)):
        candidate = f"{base} ({i}){ext}"
        i += 1
        if i > 500:
            candidate = f"{base}__{uuid.uuid4().hex}{ext}"
            break
    return candidate


def _is_bizreg_meta(meta: Any) -> bool:
    return isinstance(meta, dict) and ("biz_reg" in meta)


def _looks_like_bizreg_name(name: str) -> bool:
    raw = (name or "").strip()
    if not raw:
        return False
    if "Business profileRegistration" in raw:
        return True
    lower = raw.lower()
    if "biz_reg" in lower or "bizreg" in lower:
        return True
    if "business registration" in lower or "business_registration" in lower:
        return True
    return False


def resolve_attachment_client_id_for_crm_client(crm_client) -> int:
    """
    unified  CRM client.id  .
    non-unified () invoice client id(external_invoice_client_id / billing_clients.ipm_client_id) .
    """
    try:
        if unified_clients_enabled():
            return int(crm_client.id)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="client_attachment_service.resolve_client_id.unified",
            log_key="client_attachment_service.resolve_client_id.unified",
            log_window_seconds=300,
        )

    # non-unified: CRM -> invoice link   
    ext = getattr(crm_client, "external_invoice_client_id", None)
    try:
        if ext:
            return int(ext)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="client_attachment_service.resolve_client_id.external",
            log_key="client_attachment_service.resolve_client_id.external",
            log_window_seconds=300,
        )

    # billing_clients(ipm_client_id) 
    try:
        tbl = _safe_table_name(_actual_table_name("clients"))  # non-unified billing_clients
        t = _clients_table(tbl)
        stmt = (
            select(t.c.id)
            .where(t.c.ipm_client_id == int(crm_client.id))
            .order_by(desc(t.c.id))
            .limit(1)
        )
        row = db.session.execute(
            stmt,
        ).fetchone()
        if row:
            return int(row[0])
    except Exception:
        try:
            db.session.rollback()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="client_attachment_service.resolve_client_id.rollback",
                log_key="client_attachment_service.resolve_client_id.rollback",
                log_window_seconds=300,
            )

    #  fallback: CRM id
    return int(crm_client.id)


def _latest_bizreg_attachment_row(attachment_client_id: int) -> Optional[Dict[str, Any]]:
    tbl = _safe_table_name(
        _actual_table_name("client_attachments")
    )  #  billing_client_attachments
    try:
        t = _client_attachments_table(tbl)
        stmt = (
            select(
                t.c.id,
                t.c.client_id,
                t.c.original_name,
                t.c.stored_name,
                t.c.content_type,
                t.c.size,
                t.c.uploaded_at,
                t.c.analysis_meta,
                t.c.uploaded_by,
            )
            .where(t.c.client_id == int(attachment_client_id))
            .order_by(desc(t.c.uploaded_at), desc(t.c.id))
            .limit(50)
        )
        rows = (
            db.session.execute(
                stmt,
            )
            .mappings()
            .all()
        )
    except Exception:
        try:
            db.session.rollback()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="client_attachment_service.latest_bizreg.rollback",
                log_key="client_attachment_service.latest_bizreg.rollback",
                log_window_seconds=300,
            )
        return None

    # 1) biz_reg  (analysis_meta biz_reg ) 
    fallback: list[tuple[dict, Any]] = []
    for r in rows:
        meta = safe_json_parse(r.get("analysis_meta"), default=None)
        if _is_bizreg_meta(meta):
            out = dict(r)
            out["analysis_meta_parsed"] = meta
            out["source"] = "client_attachments"
            return out
        if _looks_like_bizreg_name(r.get("stored_name") or "") or _looks_like_bizreg_name(
            r.get("original_name") or ""
        ):
            fallback.append((r, meta))

    # 2) Legacy/migration fallback: File name (: biz_reg_*, Business document File name)
    if fallback:
        r, meta = fallback[0]
        out = dict(r)
        out["analysis_meta_parsed"] = (
            meta if isinstance(meta, dict) else {"biz_reg": {"source": "filename"}}
        )
        out["source"] = "client_attachments"
        return out
    return None


def get_bizreg_attachment_for_crm_client(
    crm_client,
    *,
    verify_exists: bool = False,
    allow_repair: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    CRM from Display:
    1) client_attachments  biz_reg 
    2) if none client.extra.biz_reg_file(?) fallback
    """
    att_cid = resolve_attachment_client_id_for_crm_client(crm_client)
    row = _latest_bizreg_attachment_row(att_cid)
    if row:
        row["attachment_client_id"] = att_cid
        info = row
    else:
        info = None

    if info is None:
        extra = getattr(crm_client, "extra", None) or {}
        if isinstance(extra, dict):
            legacy = extra.get("biz_reg_file")
            if isinstance(legacy, dict) and legacy.get("stored_name"):
                info = {
                    "source": "legacy_extra",
                    "attachment_client_id": att_cid,
                    "original_name": legacy.get("original_name") or legacy.get("stored_name"),
                    "stored_name": legacy.get("stored_name"),
                    "uploaded_at": legacy.get("uploaded_at"),
                }

    if not info or not verify_exists:
        return info

    stored = info.get("stored_name") or ""
    att_cid_val = int(info.get("attachment_client_id") or att_cid or 0)
    crm_id = getattr(crm_client, "id", None)
    if not stored or att_cid_val <= 0:
        return None

    path = resolve_client_attachment_file_path(
        att_cid_val,
        str(stored),
        crm_client_id=int(crm_id) if crm_id is not None else None,
        include_legacy_crm=True,
        repair=allow_repair,
    )
    if path:
        return info

    stream, _ = open_client_attachment_stream(
        att_cid_val,
        str(stored),
        crm_client_id=int(crm_id) if crm_id is not None else None,
        include_legacy_crm=True,
    )
    if stream is not None:
        try:
            stream.close()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="client_attachment_service.get_bizreg_attachment_for_crm_client.close_stream",
                log_key="client_attachment_service.get_bizreg_attachment_for_crm_client.close_stream",
                log_window_seconds=300,
            )
        return info

    return None


def save_bizreg_attachment_for_crm_client(
    crm_client,
    file_storage,
    *,
    uploaded_by: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    CRM Upload invoice client_attachments / Save.
    """
    if not file_storage or not getattr(file_storage, "filename", ""):
        return None

    filename = str(file_storage.filename or "").strip()
    ext = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    if ext and ext not in ALLOWED_BIZREG_EXTS:
        raise ValueError("  Business document File .")

    max_bytes = max_attachment_bytes()
    if max_bytes:
        try:
            content_len = int(getattr(file_storage, "content_length", 0) or 0)
        except Exception:
            content_len = 0
        if content_len and content_len > max_bytes:
            raise UploadTooLargeError("File  .")

    att_cid = resolve_attachment_client_id_for_crm_client(crm_client)
    dst_dir = client_attachment_dir(att_cid)
    root_dir = Path(_attachments_root()).resolve()
    dst_dir_path = Path(dst_dir).resolve()
    try:
        rel_dir = dst_dir_path.relative_to(root_dir)
    except ValueError as exc:
        raise ValueError("invalid attachment path") from exc
    safe_name = _sanitize_filename_preserve_unicode(filename)
    stored_name = _unique_stored_name(dst_dir, safe_name)
    rel_path = rel_dir / stored_name
    service = FileAssetService(upload_root=root_dir)

    # 1) File Save
    stored = service.store_upload_to_path(
        file_storage,
        rel_path=rel_path,
        overwrite=True,
        max_bytes=max_bytes or None,
    )
    dst_path = None
    if stored.abs_path is not None:
        dst_path = str(stored.abs_path)
    else:
        try:
            dst_path = str(service.abs_path(stored.rel_path))
        except Exception:
            dst_path = None
    size = int(stored.byte_size or 0)
    if dst_path:
        try:
            _validate_saved_attachment_path(
                dst_path,
                filename=filename,
                allowed_exts=ALLOWED_BIZREG_EXTS,
            )
        except Exception:
            try:
                service.delete_physical_file(stored.rel_path)
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="client_attachment_service.bizreg.validate.cleanup_orphan",
                    log_key="client_attachment_service.bizreg.validate.cleanup_orphan",
                    log_window_seconds=300,
                )
            raise

    # 2) biz_reg analysis(meta biz_reg    “”  )
    content_type = getattr(file_storage, "mimetype", "") or ""
    analysis_meta = json.dumps({"biz_reg": {}}, ensure_ascii=False)

    tbl = _safe_table_name(_actual_table_name("client_attachments"))
    params = {
        "client_id": int(att_cid),
        "original_name": filename,
        "stored_name": stored_name,
        "content_type": content_type,
        "size": int(size),
        "analysis_meta": analysis_meta,
        "uploaded_by": int(uploaded_by) if uploaded_by is not None else None,
    }

    def _insert_attachment_record() -> int | None:
        dialect = ""
        try:
            dialect = (db.engine.dialect.name or "").lower()
        except Exception:
            dialect = ""

        if dialect.startswith("postgres"):
            t = _client_attachments_table(tbl)
            row = db.session.execute(
                insert(t).values(**params).returning(t.c.id),
            ).fetchone()
            if row:
                return int(row[0])
            return None

        t = _client_attachments_table(tbl)
        db.session.execute(
            insert(t).values(**params),
        )
        try:
            row = db.session.execute(text("SELECT last_insert_rowid()")).fetchone()
            if row:
                return int(row[0])
        except Exception:
            return None
        return None

    # 3) DB insert (commit from)
    att_id = None
    try:
        att_id = _insert_attachment_record()
    except OperationalError as exc:
        # Long-running LLM analysis can leave the previous DB connection stale.
        # Drop the scoped session and retry the insert once with a fresh connection.
        msg = str(exc).lower()
        if (
            "server closed the connection unexpectedly" not in msg
            and "closed the connection" not in msg
            and "connection was closed" not in msg
            and "connection not open" not in msg
        ):
            raise
        try:
            db.session.rollback()
        except Exception as session_exc:
            report_swallowed_exception(
                session_exc,
                context="client_attachment_service.retry_insert.rollback",
                log_key="client_attachment_service.retry_insert.rollback",
                log_window_seconds=300,
            )
        try:
            db.session.remove()
        except Exception as session_exc:
            report_swallowed_exception(
                session_exc,
                context="client_attachment_service.retry_insert.remove",
                log_key="client_attachment_service.retry_insert.remove",
                log_window_seconds=300,
            )
        try:
            att_id = _insert_attachment_record()
        except Exception:
            try:
                service.delete_physical_file(stored.rel_path)
            except Exception as cleanup_exc:
                report_swallowed_exception(
                    cleanup_exc,
                    context="client_attachment_service.cleanup_orphan",
                    log_key="client_attachment_service.cleanup_orphan",
                    log_window_seconds=300,
                )
            raise
    except Exception:
        # DB insert Failed  File orphan 
        try:
            service.delete_physical_file(stored.rel_path)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="client_attachment_service.cleanup_orphan",
                log_key="client_attachment_service.cleanup_orphan",
                log_window_seconds=300,
            )
        raise

    return {
        "source": "client_attachments",
        "id": att_id,
        "attachment_client_id": att_cid,
        "original_name": filename,
        "stored_name": stored_name,
        "content_type": content_type,
        "size": size,
        "analysis_meta": analysis_meta,
    }


def list_client_attachments_for_crm_client(
    crm_client,
    *,
    limit: int = 200,
) -> tuple[int, list[dict[str, Any]]]:
    """List client attachments for CRM view using invoice attachment storage."""
    att_cid = resolve_attachment_client_id_for_crm_client(crm_client)
    items: list[dict[str, Any]] = []
    if not att_cid:
        return int(att_cid or 0), items

    try:
        safe_limit = int(limit)
    except Exception:
        safe_limit = 200
    safe_limit = max(1, min(safe_limit, 500))

    tbl = _safe_table_name(_actual_table_name("client_attachments"))
    try:
        t = _client_attachments_table(tbl)
        stmt = (
            select(
                t.c.id,
                t.c.client_id,
                t.c.original_name,
                t.c.stored_name,
                t.c.content_type,
                t.c.size,
                t.c.uploaded_at,
                t.c.analysis_meta,
            )
            .where(t.c.client_id == int(att_cid))
            .order_by(desc(t.c.uploaded_at), desc(t.c.id))
            .limit(int(safe_limit))
        )
        rows = (
            db.session.execute(
                stmt,
            )
            .mappings()
            .all()
        )
    except Exception:
        try:
            db.session.rollback()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="client_attachment_service.list.rollback",
                log_key="client_attachment_service.list.rollback",
                log_window_seconds=300,
            )
        return int(att_cid), items

    for r in rows:
        meta = safe_json_parse(r.get("analysis_meta"), default=None)
        is_biz = (
            _is_bizreg_meta(meta)
            or _looks_like_bizreg_name(r.get("stored_name") or "")
            or _looks_like_bizreg_name(r.get("original_name") or "")
        )
        size_val = r.get("size")
        items.append(
            {
                "id": int(r.get("id") or 0),
                "client_id": int(r.get("client_id") or att_cid),
                "original_name": r.get("original_name") or "",
                "stored_name": r.get("stored_name") or "",
                "content_type": r.get("content_type") or "",
                "size": int(size_val) if size_val is not None else None,
                "uploaded_at": r.get("uploaded_at"),
                "analysis_meta": meta,
                "is_biz_reg": bool(is_biz),
            }
        )

    return int(att_cid), items


def save_client_attachment_for_crm_client(
    crm_client,
    file_storage,
    *,
    uploaded_by: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Save a generic client attachment into billing_client_attachments + uploads/clients."""
    if not file_storage or not getattr(file_storage, "filename", ""):
        return None

    filename = str(file_storage.filename or "").strip()
    if not is_allowed_attachment(filename):
        raise ValueError("  File .")

    max_bytes = max_attachment_bytes()
    if max_bytes:
        try:
            content_len = int(getattr(file_storage, "content_length", 0) or 0)
        except Exception:
            content_len = 0
        if content_len and content_len > max_bytes:
            raise UploadTooLargeError("File  .")

    att_cid = resolve_attachment_client_id_for_crm_client(crm_client)
    dst_dir = client_attachment_dir(att_cid)
    root_dir = Path(_attachments_root()).resolve()
    dst_dir_path = Path(dst_dir).resolve()
    try:
        rel_dir = dst_dir_path.relative_to(root_dir)
    except ValueError as exc:
        raise ValueError("invalid attachment path") from exc

    safe_name = _sanitize_filename_preserve_unicode(filename)
    stored_name = _unique_stored_name(dst_dir, safe_name)
    rel_path = rel_dir / stored_name
    service = FileAssetService(upload_root=root_dir)

    stored = service.store_upload_to_path(
        file_storage,
        rel_path=rel_path,
        overwrite=True,
        max_bytes=max_bytes or None,
    )
    size = int(stored.byte_size or 0)
    dst_path = None
    if stored.abs_path is not None:
        dst_path = str(stored.abs_path)
    else:
        try:
            dst_path = str(service.abs_path(stored.rel_path))
        except Exception:
            dst_path = None
    if dst_path:
        try:
            _validate_saved_attachment_path(
                dst_path,
                filename=filename,
                allowed_exts=_allowed_attachment_exts(),
            )
        except Exception:
            try:
                service.delete_physical_file(stored.rel_path)
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="client_attachment_service.save.validate.cleanup_orphan",
                    log_key="client_attachment_service.save.validate.cleanup_orphan",
                    log_window_seconds=300,
                )
            raise
    content_type = getattr(file_storage, "mimetype", "") or ""

    analysis_meta = None
    if _looks_like_bizreg_name(filename):
        analysis_meta = json.dumps({"biz_reg": {"source": "filename"}}, ensure_ascii=False)

    tbl = _safe_table_name(_actual_table_name("client_attachments"))
    params = {
        "client_id": int(att_cid),
        "original_name": filename,
        "stored_name": stored_name,
        "content_type": content_type,
        "size": int(size),
        "analysis_meta": analysis_meta,
        "uploaded_by": int(uploaded_by) if uploaded_by is not None else None,
    }

    att_id = None
    try:
        dialect = ""
        try:
            dialect = (db.engine.dialect.name or "").lower()
        except Exception:
            dialect = ""

        if dialect.startswith("postgres"):
            t = _client_attachments_table(tbl)
            row = db.session.execute(
                insert(t).values(**params).returning(t.c.id),
            ).fetchone()
            if row:
                att_id = int(row[0])
        else:
            t = _client_attachments_table(tbl)
            db.session.execute(
                insert(t).values(**params),
            )
            try:
                row = db.session.execute(text("SELECT last_insert_rowid()")).fetchone()
                if row:
                    att_id = int(row[0])
            except Exception:
                att_id = None
    except Exception:
        try:
            service.delete_physical_file(stored.rel_path)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="client_attachment_service.save.cleanup_orphan",
                log_key="client_attachment_service.save.cleanup_orphan",
                log_window_seconds=300,
            )
        raise

    return {
        "source": "client_attachments",
        "id": att_id,
        "attachment_client_id": att_cid,
        "original_name": filename,
        "stored_name": stored_name,
        "content_type": content_type,
        "size": size,
        "analysis_meta": analysis_meta,
    }


def get_client_attachment_for_crm_client(
    crm_client, attachment_id: int
) -> Optional[Dict[str, Any]]:
    att_cid = resolve_attachment_client_id_for_crm_client(crm_client)
    tbl = _safe_table_name(_actual_table_name("client_attachments"))
    try:
        t = _client_attachments_table(tbl)
        stmt = (
            select(
                t.c.id,
                t.c.client_id,
                t.c.original_name,
                t.c.stored_name,
                t.c.content_type,
                t.c.size,
                t.c.uploaded_at,
                t.c.analysis_meta,
            )
            .where(t.c.id == int(attachment_id))
            .where(t.c.client_id == int(att_cid))
            .limit(1)
        )
        row = (
            db.session.execute(
                stmt,
            )
            .mappings()
            .first()
        )
    except Exception:
        try:
            db.session.rollback()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="client_attachment_service.get.rollback",
                log_key="client_attachment_service.get.rollback",
                log_window_seconds=300,
            )
        return None
    if not row:
        return None
    out = dict(row)
    out["attachment_client_id"] = int(att_cid)
    out["analysis_meta_parsed"] = safe_json_parse(row.get("analysis_meta"), default=None)
    return out


def delete_client_attachment_for_crm_client(crm_client, attachment_id: int) -> bool:
    info = get_client_attachment_for_crm_client(crm_client, attachment_id)
    if not info:
        return False

    att_cid = int(info.get("attachment_client_id") or 0)
    stored = os.path.basename(str(info.get("stored_name") or ""))
    if stored and stored == (info.get("stored_name") or ""):
        paths: list[str] = []
        primary_dir = client_attachment_dir(att_cid)
        primary_path = os.path.join(primary_dir, stored)
        if os.path.exists(primary_path):
            paths.append(primary_path)
        else:
            alt_path = resolve_client_attachment_file_path(
                att_cid,
                stored,
                crm_client_id=getattr(crm_client, "id", None),
                include_legacy_crm=True,
                repair=False,
            )
            if alt_path:
                paths.append(alt_path)

        for path in paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="client_attachment_service.delete.remove_file",
                    log_key="client_attachment_service.delete.remove_file",
                    log_window_seconds=300,
                )

        # If attachments are stored in non-local backend (e.g., S3), attempt delete there too.
        try:
            backend = get_storage_backend()
            if not isinstance(backend, LocalStorageBackend):
                backend.delete(f"client_{att_cid}/{stored}")
                crm_id = getattr(crm_client, "id", None)
                if crm_id is not None:
                    backend.delete(f"crm_client_{int(crm_id)}/{stored}")
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="client_attachment_service.delete.backend",
                log_key="client_attachment_service.delete.backend",
                log_window_seconds=300,
            )

    tbl = _safe_table_name(_actual_table_name("client_attachments"))
    t = _client_attachments_table(tbl)
    db.session.execute(
        delete(t).where(t.c.id == int(attachment_id)),
    )
    return True


def migrate_legacy_bizreg_for_crm_client(
    crm_client,
    *,
    uploaded_by: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Legacy: client.extra.biz_reg_file + uploads/clients/crm_client_{id}/... 
    Current table(uploads/clients/client_{attachment_client_id} + client_attachments ) .
    """
    extra = getattr(crm_client, "extra", None) or {}
    if not isinstance(extra, dict):
        return None
    legacy = extra.get("biz_reg_file")
    if not isinstance(legacy, dict):
        return None

    stored = (legacy.get("stored_name") or "").strip()
    if not stored:
        return None

    src_dir = legacy_crm_bizreg_dir(int(crm_client.id))
    src_path = os.path.join(src_dir, stored)
    if not os.path.exists(src_path):
        return None

    att_cid = resolve_attachment_client_id_for_crm_client(crm_client)
    dst_dir = client_attachment_dir(att_cid)

    original_name = (legacy.get("original_name") or stored).strip()
    safe_name = _sanitize_filename_preserve_unicode(original_name)
    dst_name = _unique_stored_name(dst_dir, safe_name)
    dst_path = os.path.join(dst_dir, dst_name)

    try:
        shutil.move(src_path, dst_path)
    except Exception:
        shutil.copy2(src_path, dst_path)

    size = 0
    try:
        size = os.path.getsize(dst_path)
    except Exception:
        size = 0

    # meta Create()
    analysis_meta = json.dumps({"biz_reg": {}}, ensure_ascii=False)

    tbl = _safe_table_name(_actual_table_name("client_attachments"))
    params = {
        "client_id": int(att_cid),
        "original_name": original_name,
        "stored_name": dst_name,
        "content_type": "",
        "size": int(size),
        "analysis_meta": analysis_meta,
        "uploaded_by": int(uploaded_by) if uploaded_by is not None else None,
    }

    # insert (commit from)
    att_id = None
    dialect = ""
    try:
        dialect = (db.engine.dialect.name or "").lower()
    except Exception:
        dialect = ""
    if dialect.startswith("postgres"):
        t = _client_attachments_table(tbl)
        row = db.session.execute(
            insert(t).values(**params).returning(t.c.id),
        ).fetchone()
        if row:
            att_id = int(row[0])
    else:
        t = _client_attachments_table(tbl)
        db.session.execute(
            insert(t).values(**params),
        )
        try:
            row = db.session.execute(text("SELECT last_insert_rowid()")).fetchone()
            if row:
                att_id = int(row[0])
        except Exception:
            att_id = None

    # CRM extra to Updated( client_{att_cid} )
    extra2 = dict(extra)
    extra2["biz_reg_file"] = {
        "original_name": original_name,
        "stored_name": dst_name,
        "uploaded_at": legacy.get("uploaded_at"),
        "attachment_client_id": att_cid,
        "attachment_id": att_id,
        "migrated": True,
    }
    crm_client.extra = extra2
    flag_modified(crm_client, "extra")

    return {
        "source": "client_attachments",
        "id": att_id,
        "attachment_client_id": att_cid,
        "original_name": original_name,
        "stored_name": dst_name,
        "analysis_meta": analysis_meta,
        "migrated": True,
    }
