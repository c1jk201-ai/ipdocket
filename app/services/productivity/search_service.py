from __future__ import annotations

import re
from typing import Any, Optional

from flask import current_app
from flask_login import current_user
from sqlalchemy import and_, func, inspect, or_

from app.extensions import db
from app.models.case_flat_index import CaseFlatIndex
from app.models.client import Client
from app.models.docket import DocketItem
from app.models.document import Document, Folder
from app.models.matter import Matter
from app.models.permissions import Permissions
from app.models.user_saved_view import UserSavedView
from app.services.billing.subsystem import billing_subsystem_enabled, billing_subsystem_ready
from app.services.productivity.utils import (
    check_can_access_matter_id,
    get_docket_pk,
    get_docket_title,
    has_attr_safe,
)
from app.utils.error_logging import report_swallowed_exception
from app.utils.search import (
    compact_search_text,
    extract_positive_search_terms,
    matches_search_expression,
    parse_search_expression,
    sqlalchemy_contains_query,
)
from app.utils.workflow_roles import workflow_user_filter


def _uses_compact_index_query(value: object) -> bool:
    return False

# Optional Imports
try:
    from app.models.workflow import Workflow
except ImportError:
    Workflow = None

try:
    from app.models.email_automation import EmailMessage
except ImportError:
    EmailMessage = None

try:
    from app.services.billing.invoice_services import InvoiceService
except ImportError:
    InvoiceService = None

try:
    from app.services.productivity.view_service import build_view_url
except ImportError:
    build_view_url = None

# Role constants
try:
    from app.models.user import (
        ROLE_ADMIN,
        ROLE_LEAD_ATTORNEY,
        ROLE_MGMT_DIRECTOR,
        ROLE_MGMT_STAFF,
        ROLE_PARTNER_ATTORNEY,
        ROLE_PATENT_STAFF,
    )
except ImportError:
    ROLE_ADMIN = "admin"
    ROLE_MGMT_DIRECTOR = "mgmt_director"
    ROLE_MGMT_STAFF = "mgmt_staff"
    ROLE_LEAD_ATTORNEY = "lead_attorney"
    ROLE_PARTNER_ATTORNEY = "partner_attorney"
    ROLE_PATENT_STAFF = "patent_staff"


_CASE_INDEX_AVAILABLE: Optional[bool] = None
_MAIL_TABLE_AVAILABLE: Optional[bool] = None


def _case_index_available() -> bool:
    global _CASE_INDEX_AVAILABLE
    if _CASE_INDEX_AVAILABLE is not None:
        return _CASE_INDEX_AVAILABLE
    try:
        try:
            eng = db.get_engine(current_app)
        except Exception:
            eng = db.engine
        _CASE_INDEX_AVAILABLE = inspect(eng).has_table("case_flat_index")
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="search_service._case_index_available",
            log_key="search_service._case_index_available",
            log_window_seconds=300,
        )
        _CASE_INDEX_AVAILABLE = False
    return _CASE_INDEX_AVAILABLE


def _mail_table_available() -> bool:
    global _MAIL_TABLE_AVAILABLE
    if _MAIL_TABLE_AVAILABLE is not None:
        return _MAIL_TABLE_AVAILABLE
    try:
        try:
            eng = db.get_engine(current_app)
        except Exception:
            eng = db.engine
        _MAIL_TABLE_AVAILABLE = inspect(eng).has_table("email_message")
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="search_service._mail_table_available",
            log_key="search_service._mail_table_available",
            log_window_seconds=300,
        )
        _MAIL_TABLE_AVAILABLE = False
    return _MAIL_TABLE_AVAILABLE


def _build_subtitle(*parts: Any) -> str:
    out: list[str] = []
    for part in parts:
        val = str(part or "").strip()
        if not val or val in out:
            continue
        out.append(val)
    return " / ".join(out)


def _ilike(field, q: str):
    return sqlalchemy_contains_query(field, q)


_FIELD_ALIASES = {
    "applicant": "applicant",
    "Applicant": "applicant",
    "client": "client_name",
    "Client": "client_name",
    "Client": "client_name",
    "party": "party",
    "": "party",
    "our_ref": "our_ref",
    "ref": "our_ref",
    "Matter reference": "our_ref",
    "your_ref": "your_ref",
    "client_ref": "your_ref",
    "matter_id": "matter_id",
    "Matterid": "matter_id",
    "id": "matter_id",
    "title": "right_name",
    "name": "right_name",
    "right_name": "right_name",
    "Title": "right_name",
    "application_no": "application_no",
    "app_no": "application_no",
    "appl_no": "application_no",
    "Application No.": "application_no",
    "registration_no": "registration_no",
    "reg_no": "registration_no",
    "Registration No.": "registration_no",
    "publication_no": "publication_no",
    "pub_no": "publication_no",
    "Publication No.": "publication_no",
    "attorney": "attorney",
    "": "attorney",
    "handler": "handler",
    "Contact": "handler",
    "manager": "manager",
    "": "manager",
    "inventor": "inventor",
    "Inventor": "inventor",
    "status": "status",
    "Status": "status",
    "email": "email",
    "mail": "email",
    "": "email",
}

_FIELD_QUERY_RE = re.compile(r"(?P<key>[^\s:=]+)\s*[:=]\s*(?P<val>\"[^\"]+\"|'[^']+'|[^\s]+)")


def _normalize_identifier(raw: str) -> str:
    if not raw:
        return ""
    return compact_search_text(raw).upper()


def _normalized_field(field):
    expr = func.upper(field)
    for ch in ("-", " ", "/", ".", "_"):
        expr = func.replace(expr, ch, "")
    return expr


def _looks_like_identifier(raw: str) -> bool:
    s = (raw or "").strip()
    if not s:
        return False
    if "@" in s:
        return False
    s = re.sub(r"\s+", "", s)
    if re.search(r"\d", s) and len(s) >= 5:
        return True
    if re.match(r"[A-Z]{2,}[-_]?\d+", s, flags=re.I):
        return True
    return False


def _parse_search_query(raw: str) -> tuple[str, dict[str, list[str]]]:
    raw = (raw or "").strip()
    if not raw:
        return "", {}

    fields: dict[str, list[str]] = {}
    stripped = raw
    for m in _FIELD_QUERY_RE.finditer(raw):
        key = (m.group("key") or "").strip()
        val = (m.group("val") or "").strip().strip("'\"")
        if not key or not val:
            continue
        alias = _FIELD_ALIASES.get(key.lower()) or _FIELD_ALIASES.get(key)
        if not alias:
            continue
        fields.setdefault(alias, []).append(val)
        stripped = stripped.replace(m.group(0), " ")

    stripped = re.sub(r"\s+", " ", stripped).strip()
    if not fields and stripped:
        parts = stripped.split()
        if len(parts) > 1:
            alias = _FIELD_ALIASES.get(parts[0].lower()) or _FIELD_ALIASES.get(parts[0])
            if alias:
                fields.setdefault(alias, []).append(" ".join(parts[1:]))
                stripped = ""
    return stripped, fields


def _build_search_item(
    payload: dict,
    *,
    search_text: str = "",
    search_fields: dict[str, object] | None = None,
) -> dict:
    item = dict(payload)
    item["_search_text"] = str(search_text or "").strip()
    item["_search_fields"] = dict(search_fields or {})
    return item


def _strip_search_meta(item: dict) -> dict:
    return {key: value for key, value in item.items() if not str(key).startswith("_")}


def _item_matches_expression(item: dict, expression) -> bool:
    search_text = str(item.get("_search_text") or "").strip()
    if not search_text:
        search_text = _build_subtitle(item.get("title"), item.get("subtitle"))
    return matches_search_expression(
        search_text,
        expression,
        field_values=item.get("_search_fields") or {},
    )


def _merge_bucket_searches(fetcher, queries: list[str], *, limit: int) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for query in queries:
        rows = fetcher(q=query, limit=limit)
        for row in rows:
            row_type = str(row.get("type") or "")
            row_id = str(row.get("id") or "")
            row_url = str(row.get("url") or "")
            key = (row_type, row_id, row_url)
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
            if len(out) >= limit:
                return out
    return out


def _can_access_folder(folder: Folder) -> bool:
    if not folder:
        return False
    # Use has_attr_safe or try/except
    if has_attr_safe(folder, "is_team"):
        try:
            if folder.is_team:
                return True
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="productivity.search_service._can_access_folder.is_team",
                log_key="productivity.search_service._can_access_folder.is_team",
                log_window_seconds=300,
            )

    if current_user is None or not getattr(current_user, "is_authenticated", False):
        return False
    return getattr(folder, "owner_id", None) == getattr(current_user, "id", None)


def _can_view_all_workflows() -> bool:
    if current_user is None or not getattr(current_user, "is_authenticated", False):
        return False
    for fn in ("can_view_all_work_deadlines", "can_view_all_mgmt_deadlines", "is_mgmt_role"):
        try:
            checker = getattr(current_user, fn, None)
            if callable(checker) and checker():
                return True
        except Exception:
            continue
    return False


def _has_permission(perm: str) -> bool:
    if current_user is None or not getattr(current_user, "is_authenticated", False):
        return False
    checker = getattr(current_user, "has_permission", None)
    if callable(checker):
        try:
            return bool(checker(perm))
        except Exception:
            return False
    return False


def _apply_docket_visibility_filter(q):
    if current_user is None or not getattr(current_user, "is_authenticated", False):
        from sqlalchemy import false

        return q.filter(false())

    user_role = (getattr(current_user, "role", None) or "").strip()
    staff_pid = (getattr(current_user, "staff_party_id", None) or "").strip()
    if not staff_pid:
        from sqlalchemy import false

        return q.filter(false())

    show_all_mgmt = False
    show_all_work = False

    # Global roles can see all tasks within their scope; everyone can see their own
    # docket items regardless of MGMT/WORK classification (case-role-based ownership).
    if user_role in (ROLE_ADMIN, ROLE_LEAD_ATTORNEY):
        show_all_mgmt = True
        show_all_work = True
    elif user_role == ROLE_MGMT_DIRECTOR:
        show_all_mgmt = True
    elif user_role == ROLE_PARTNER_ATTORNEY:
        show_all_work = True

    from app.utils.task_classification import MGMT_CATEGORIES, WORK_CATEGORIES

    visibility_conditions = []
    cat_upper = func.upper(DocketItem.category)

    # Always include "my" docket items (owner-based), even if category is outside the
    # MGMT/WORK sets (legacy data).
    visibility_conditions.append(DocketItem.owner_staff_party_id == staff_pid)

    if show_all_mgmt:
        visibility_conditions.append(cat_upper.in_(MGMT_CATEGORIES))

    if show_all_work:
        visibility_conditions.append(cat_upper.in_(WORK_CATEGORIES))

    if visibility_conditions:
        return q.filter(or_(*visibility_conditions))

    from sqlalchemy import false

    return q.filter(false())


def _search_matters(
    *, q: str, limit: int, field_terms: Optional[dict] = None, raw_query: Optional[str] = None
) -> list[dict]:
    text_q = (q or "").strip()
    raw_q = (raw_query or text_q).strip()
    field_terms = field_terms or {}
    case_index_ok = _case_index_available()
    is_compact = _uses_compact_index_query(text_q)

    def _base_query():
        if case_index_ok:
            mq = db.session.query(Matter, CaseFlatIndex).outerjoin(
                CaseFlatIndex, CaseFlatIndex.matter_id == Matter.matter_id
            )
        else:
            mq = db.session.query(Matter)
        if has_attr_safe(Matter, "is_deleted"):
            mq = mq.filter(or_(Matter.is_deleted.is_(False), Matter.is_deleted.is_(None)))
        return mq

    out: list[dict] = []
    seen: set[str] = set()

    def _append_rows(rows):
        for row in rows:
            if case_index_ok:
                m, idx = row
            else:
                m, idx = row, None
            mid = getattr(m, "matter_id", None) or getattr(m, "id", None)
            if not mid:
                continue
            mid_str = str(mid)
            if mid_str in seen:
                continue
            if not check_can_access_matter_id(mid_str, action="view"):
                continue
            our_ref = (getattr(m, "our_ref", None) or "").strip()
            your_ref = (getattr(m, "your_ref", None) or "").strip()
            old_our_ref = (getattr(m, "old_our_ref", None) or "").strip()
            right_name = (getattr(m, "right_name", None) or "").strip()
            client_name = (getattr(idx, "client_name", None) or "").strip() if idx else ""
            applicant = (getattr(idx, "applicant", None) or "").strip() if idx else ""
            application_no = (getattr(idx, "application_no", None) or "").strip() if idx else ""
            registration_no = (getattr(idx, "registration_no", None) or "").strip() if idx else ""
            publication_no = (getattr(idx, "publication_no", None) or "").strip() if idx else ""
            attorney = (getattr(idx, "attorney", None) or "").strip() if idx else ""
            handler = (getattr(idx, "handler", None) or "").strip() if idx else ""
            manager = (getattr(idx, "manager", None) or "").strip() if idx else ""
            inventor = (getattr(idx, "inventor", None) or "").strip() if idx else ""
            status_values = [
                str(getattr(m, "status_red", None) or "").strip(),
                str(getattr(m, "status_blue", None) or "").strip(),
                str(getattr(m, "inhouse_status", None) or "").strip(),
            ]

            title = our_ref or right_name or mid_str
            subtitle = _build_subtitle(
                right_name if right_name and title != right_name else "",
                client_name,
                applicant,
            )
            search_text = _build_subtitle(
                title,
                right_name,
                client_name,
                applicant,
                our_ref,
                your_ref,
                old_our_ref,
                application_no,
                registration_no,
                publication_no,
                attorney,
                handler,
                manager,
                inventor,
                *status_values,
            )
            out.append(
                _build_search_item(
                    {
                        "type": "matter",
                        "id": mid,
                        "title": title,
                        "subtitle": subtitle,
                        "url": f"/case/{mid}",
                    },
                    search_text=search_text,
                    search_fields={
                        "client_name": client_name,
                        "applicant": applicant,
                        "party": [client_name, applicant],
                        "our_ref": our_ref,
                        "your_ref": your_ref,
                        "matter_id": mid_str,
                        "right_name": right_name,
                        "application_no": application_no,
                        "registration_no": registration_no,
                        "publication_no": publication_no,
                        "attorney": attorney,
                        "handler": handler,
                        "manager": manager,
                        "inventor": inventor,
                        "status": [value for value in status_values if value],
                    },
                )
            )
            seen.add(mid_str)

    def _add_norm(clauses, field_obj, values):
        for val in values:
            norm = _normalize_identifier(val)
            if not norm:
                continue
            clauses.append(_normalized_field(field_obj).like(f"%{norm}%"))

    primary_clauses = []
    ref_terms = list(field_terms.get("our_ref", []))
    ref_terms += list(field_terms.get("your_ref", []))
    ref_terms += list(field_terms.get("matter_id", []))
    if _looks_like_identifier(raw_q):
        ref_terms.append(raw_q)

    if ref_terms:
        for field in ("our_ref", "your_ref", "old_our_ref", "matter_id"):
            if has_attr_safe(Matter, field):
                for val in ref_terms:
                    primary_clauses.append(_ilike(getattr(Matter, field), val))
                _add_norm(primary_clauses, getattr(Matter, field), ref_terms)

    if case_index_ok:
        if field_terms.get("application_no") and has_attr_safe(CaseFlatIndex, "application_no"):
            _add_norm(primary_clauses, CaseFlatIndex.application_no, field_terms["application_no"])
        if field_terms.get("registration_no") and has_attr_safe(CaseFlatIndex, "registration_no"):
            _add_norm(
                primary_clauses, CaseFlatIndex.registration_no, field_terms["registration_no"]
            )
        if field_terms.get("publication_no") and has_attr_safe(CaseFlatIndex, "publication_no"):
            _add_norm(primary_clauses, CaseFlatIndex.publication_no, field_terms["publication_no"])

        party_terms = []
        party_terms.extend(field_terms.get("client_name", []))
        party_terms.extend(field_terms.get("applicant", []))
        party_terms.extend(field_terms.get("party", []))
        if party_terms:
            if has_attr_safe(CaseFlatIndex, "client_name"):
                for val in party_terms:
                    primary_clauses.append(_ilike(CaseFlatIndex.client_name, val))
            if has_attr_safe(CaseFlatIndex, "applicant"):
                for val in party_terms:
                    primary_clauses.append(_ilike(CaseFlatIndex.applicant, val))

    if primary_clauses:
        rows = (
            _base_query()
            .filter(or_(*primary_clauses))
            .order_by(Matter.matter_id.desc())
            .limit(limit)
            .all()
        )
        _append_rows(rows)

    if text_q and len(out) < limit:
        secondary_clauses = []
        for field_name in ("our_ref", "your_ref", "old_our_ref", "right_name", "matter_type"):
            if has_attr_safe(Matter, field_name):
                secondary_clauses.append(_ilike(getattr(Matter, field_name), text_q))

        if case_index_ok:
            if is_compact and has_attr_safe(CaseFlatIndex, "search_compact"):
                secondary_clauses.append(
                    _ilike(CaseFlatIndex.search_compact, compact_search_text(text_q))
                )
            elif has_attr_safe(CaseFlatIndex, "search_text"):
                secondary_clauses.append(_ilike(CaseFlatIndex.search_text, text_q))
            if has_attr_safe(CaseFlatIndex, "client_name"):
                secondary_clauses.append(_ilike(CaseFlatIndex.client_name, text_q))
            if has_attr_safe(CaseFlatIndex, "applicant"):
                secondary_clauses.append(_ilike(CaseFlatIndex.applicant, text_q))
            if _looks_like_identifier(text_q):
                for field in ("application_no", "registration_no", "publication_no"):
                    if has_attr_safe(CaseFlatIndex, field):
                        _add_norm(secondary_clauses, getattr(CaseFlatIndex, field), [text_q])

        if secondary_clauses:
            rows = (
                _base_query()
                .filter(or_(*secondary_clauses))
                .order_by(Matter.matter_id.desc())
                .limit(max(0, limit - len(out)))
                .all()
            )
            _append_rows(rows)

    return out


def _search_clients(*, q: str, limit: int, field_terms: Optional[dict] = None) -> list[dict]:
    if not _has_permission(Permissions.MENU_CRM):
        return []

    text_q = (q or "").strip()
    field_terms = field_terms or {}

    name_terms = []
    name_terms.extend(field_terms.get("client_name", []))
    name_terms.extend(field_terms.get("party", []))
    if text_q:
        name_terms.append(text_q)

    email_terms = []
    email_terms.extend(field_terms.get("email", []))
    if text_q and "@" in text_q:
        email_terms.append(text_q)

    if not name_terms and not email_terms:
        return []

    cq = db.session.query(Client)
    if has_attr_safe(Client, "is_deleted"):
        cq = cq.filter(or_(Client.is_deleted.is_(False), Client.is_deleted.is_(None)))

    clauses = []
    for term in name_terms:
        if not term:
            continue
        for field_name in (
            "name",
            "phone",
            "registration_number",
            "biz_reg_number",
            "biz_company_name",
            "contact_person",
            "manager",
        ):
            if has_attr_safe(Client, field_name):
                clauses.append(_ilike(getattr(Client, field_name), term))

    for term in email_terms:
        if not term:
            continue
        for field_name in ("email", "biz_tax_invoice_email"):
            if has_attr_safe(Client, field_name):
                clauses.append(_ilike(getattr(Client, field_name), term))

    if not clauses:
        return []

    rows = cq.filter(or_(*clauses)).order_by(Client.id.desc()).limit(limit).all()
    out: list[dict] = []
    for c in rows:
        cid = getattr(c, "id", None)
        if not cid:
            continue
        name = (getattr(c, "name", None) or "").strip() or f"Client {cid}"
        email = (getattr(c, "email", None) or "").strip()
        biz_email = (getattr(c, "biz_tax_invoice_email", None) or "").strip()
        biz_name = (getattr(c, "biz_company_name", None) or "").strip()
        registration_no = (getattr(c, "registration_number", None) or "").strip()
        biz_reg_no = (getattr(c, "biz_reg_number", None) or "").strip()
        subtitle = _build_subtitle(
            biz_name,
            registration_no,
            biz_reg_no,
            email,
        )
        out.append(
            _build_search_item(
                {
                    "type": "client",
                    "id": cid,
                    "title": name,
                    "subtitle": subtitle,
                    "url": f"/crm/clients/{cid}",
                },
                search_text=_build_subtitle(
                    name, biz_name, registration_no, biz_reg_no, email, biz_email
                ),
                search_fields={
                    "client_name": name,
                    "party": name,
                    "email": [value for value in (email, biz_email) if value],
                },
            )
        )
    return out


def _search_invoices(*, q: str, limit: int) -> list[dict]:
    if not _has_permission(Permissions.MENU_ACCOUNTING):
        return []

    if InvoiceService is None:
        return []
    if not billing_subsystem_enabled(current_app):
        return []
    if not billing_subsystem_ready(current_app):
        return []

    q = (q or "").strip()
    if not q:
        return []

    try:
        rows = InvoiceService.search_invoices(q=q, limit=limit)
    except Exception:
        current_app.logger.exception("Invoice quick search failed")
        return []

    out: list[dict] = []
    for inv in rows:
        inv_id = inv.get("id")
        if inv_id is None:
            continue
        number = str(inv.get("number") or "").strip()
        title = number or f"Invoice #{inv_id}"
        subtitle = _build_subtitle(
            inv.get("client_name"),
            inv.get("issue_date"),
            inv.get("billing_status") or inv.get("status"),
            inv.get("payment_status"),
        )
        out.append(
            {
                "type": "invoice",
                "id": inv_id,
                "title": title,
                "subtitle": subtitle,
                "url": f"/accounting/invoice-system/invoices/{inv_id}",
            }
        )
    return out


def _search_dockets(*, q: str, limit: int) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []

    clauses = []
    for field_name in (
        "name_free",
        "name_ref",
        "name",
        "title",
        "memo",
        "category",
        "matter_id",
    ):
        if has_attr_safe(DocketItem, field_name):
            clauses.append(_ilike(getattr(DocketItem, field_name), q))

    if not clauses:
        return []

    dq = db.session.query(DocketItem, Matter).outerjoin(
        Matter, getattr(DocketItem, "matter_id", None) == getattr(Matter, "matter_id", None)
    )
    dq = _apply_docket_visibility_filter(dq)
    if has_attr_safe(DocketItem, "is_deleted"):
        dq = dq.filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
    dq = dq.filter(or_(*clauses))

    if has_attr_safe(DocketItem, "due_date"):
        dq = dq.order_by(DocketItem.due_date.asc())
    rows = dq.limit(limit).all()

    out: list[dict] = []
    for row in rows:
        d, m = row
        mid = getattr(d, "matter_id", None)
        name = get_docket_title(d)
        due = getattr(d, "due_date", None) or ""
        our_ref = (getattr(m, "our_ref", None) or "").strip() if m else ""
        right_name = (getattr(m, "right_name", None) or "").strip() if m else ""
        subtitle = _build_subtitle(our_ref, right_name, due)
        did = get_docket_pk(d)
        out.append(
            _build_search_item(
                {
                    "type": "docket",
                    "id": did,
                    "title": str(name).strip(),
                    "subtitle": subtitle,
                    "url": f"/case/{mid}#sec-due" if mid else None,
                },
                search_text=_build_subtitle(name, our_ref, right_name, due, mid),
                search_fields={
                    "matter_id": str(mid or "").strip(),
                    "our_ref": our_ref,
                    "right_name": right_name,
                },
            )
        )
    return out


def _search_workflows(*, q: str, limit: int) -> list[dict]:
    if Workflow is None:
        return []
    q = (q or "").strip()
    if not q:
        return []

    clauses = []
    for field_name in (
        "name",
        "title",
        "subject",
        "business_code",
        "code",
        "case_id",
    ):
        if has_attr_safe(Workflow, field_name):
            clauses.append(_ilike(getattr(Workflow, field_name), q))
    if not clauses:
        return []

    wq = db.session.query(Workflow, Matter).outerjoin(
        Matter, getattr(Workflow, "case_id", None) == getattr(Matter, "matter_id", None)
    )
    if not _can_view_all_workflows() and has_attr_safe(Workflow, "assignee_id"):
        wq = wq.filter(workflow_user_filter(getattr(current_user, "id", None)))
    if has_attr_safe(Workflow, "status"):
        wq = wq.filter(Workflow.status.notin_(["Completed", "Abandoned"]))
    wq = wq.filter(or_(*clauses))
    if has_attr_safe(Workflow, "due_date"):
        wq = wq.order_by(Workflow.due_date.asc())

    rows = wq.limit(limit).all()
    out: list[dict] = []
    for row in rows:
        wf, m = row
        mid = getattr(wf, "case_id", None) or getattr(wf, "matter_id", None)
        title = (
            getattr(wf, "name", None)
            or getattr(wf, "title", None)
            or getattr(wf, "subject", None)
            or "Task"
        )
        due = getattr(wf, "due_date", None) or getattr(wf, "legal_due_date", None) or ""
        our_ref = (getattr(m, "our_ref", None) or "").strip() if m else ""
        right_name = (getattr(m, "right_name", None) or "").strip() if m else ""
        subtitle = _build_subtitle(our_ref, right_name, due)
        out.append(
            _build_search_item(
                {
                    "type": "workflow",
                    "id": getattr(wf, "id", None),
                    "title": str(title).strip(),
                    "subtitle": subtitle,
                    "url": f"/case/{mid}#sec-workflow" if mid else None,
                },
                search_text=_build_subtitle(title, our_ref, right_name, due, mid),
                search_fields={
                    "matter_id": str(mid or "").strip(),
                    "our_ref": our_ref,
                    "right_name": right_name,
                },
            )
        )
    return out


def _search_documents(*, q: str, limit: int) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []
    if current_user is None or not getattr(current_user, "is_authenticated", False):
        return []

    dq = db.session.query(Document, Folder).outerjoin(Folder, Document.folder_id == Folder.id)
    if has_attr_safe(Document, "title"):
        dq = dq.filter(_ilike(Document.title, q))

    # _can_access_folder logic embedded here for query building?
    # No, we can't easily use python function in SQL query.
    # We must replicate the logic in SQL or filter post-query.
    # The original productivity_service didn't use _can_access_folder in query, it did:

    # if _has_attr(Folder, "is_team") and _has_attr(Folder, "owner_id"):
    #     dq = dq.filter(
    #         or_(Folder.is_team.is_(True), Folder.owner_id == getattr(current_user, "id", None))
    #     )

    # Let's keep that logic.
    if has_attr_safe(Folder, "is_team") and has_attr_safe(Folder, "owner_id"):
        dq = dq.filter(
            or_(Folder.is_team.is_(True), Folder.owner_id == getattr(current_user, "id", None))
        )

    rows = dq.order_by(Document.id.desc()).limit(limit).all()
    out: list[dict] = []
    for d, f in rows:
        title = (getattr(d, "title", None) or "Document").strip()
        folder_name = (getattr(f, "name", None) or "").strip() if f else ""
        subtitle = _build_subtitle(folder_name)
        ver_id = getattr(d, "current_version_id", None)
        url = f"/document/download/{ver_id}" if ver_id else "/document/"
        out.append(
            _build_search_item(
                {
                    "type": "document",
                    "id": getattr(d, "id", None),
                    "title": title,
                    "subtitle": subtitle,
                    "url": url,
                },
                search_text=_build_subtitle(title, folder_name),
            )
        )
    return out


def _search_mail(*, q: str, limit: int, field_terms: Optional[dict] = None) -> list[dict]:
    if EmailMessage is None or not _mail_table_available():
        return []
    text_q = (q or "").strip()
    field_terms = field_terms or {}

    terms = []
    if text_q:
        terms.append(text_q)
    terms.extend(field_terms.get("email", []))

    if not terms:
        return []

    clauses = []
    for term in terms:
        if not term:
            continue
        for field_name in ("subject", "from_addr", "to_text", "cc_text"):
            if has_attr_safe(EmailMessage, field_name):
                clauses.append(_ilike(getattr(EmailMessage, field_name), term))

    if not clauses:
        return []

    mq = db.session.query(EmailMessage).filter(or_(*clauses))
    if has_attr_safe(EmailMessage, "received_at"):
        mq = mq.order_by(EmailMessage.received_at.desc())
    rows = mq.limit(limit).all()

    out: list[dict] = []
    for email in rows:
        eid = getattr(email, "id", None)
        if not eid:
            continue
        received = ""
        received_at = getattr(email, "received_at", None)
        if received_at and hasattr(received_at, "strftime"):
            received = received_at.strftime("%Y-%m-%d")
        title = (
            (getattr(email, "subject", None) or "").strip()
            or (getattr(email, "from_addr", None) or "").strip()
            or ""
        )
        subtitle = _build_subtitle(
            getattr(email, "from_addr", None),
            getattr(email, "to_text", None),
            received,
        )
        out.append(
            _build_search_item(
                {
                    "type": "mail",
                    "id": eid,
                    "title": title,
                    "subtitle": subtitle,
                    "url": None,
                },
                search_text=_build_subtitle(
                    title,
                    getattr(email, "from_addr", None),
                    getattr(email, "to_text", None),
                    getattr(email, "cc_text", None),
                    received,
                ),
                search_fields={
                    "email": [
                        value
                        for value in (
                            getattr(email, "from_addr", None),
                            getattr(email, "to_text", None),
                            getattr(email, "cc_text", None),
                        )
                        if str(value or "").strip()
                    ]
                },
            )
        )
    return out


def _search_views(*, q: str, limit: int) -> list[dict]:
    q = (q or "").strip()
    if current_user is None or not getattr(current_user, "is_authenticated", False):
        return []

    uid = getattr(current_user, "id", None)
    if uid is None:
        return []

    vq = UserSavedView.query.filter_by(user_id=uid)
    if q:
        vq = vq.filter(_ilike(UserSavedView.name, q))

    rows = (
        vq.order_by(UserSavedView.is_default.desc(), UserSavedView.updated_at.desc())
        .limit(limit)
        .all()
    )
    out: list[dict] = []
    for v in rows:
        subtitle = _build_subtitle(v.module, "Default" if v.is_default else "", v.scope)
        url = build_view_url(v) if callable(build_view_url) else None
        out.append(
            _build_search_item(
                {
                    "type": "view",
                    "id": v.id,
                    "title": v.name,
                    "subtitle": subtitle,
                    "url": url,
                },
                search_text=_build_subtitle(
                    v.name, v.module, v.scope, "Default" if v.is_default else ""
                ),
            )
        )
    return out


def quick_search(*, q: str, limit: int = 20, type_filter: Optional[set[str]] = None) -> list[dict]:
    """
    Ctrl+K Search( MVP)
    - Matter/Client/Invoice + docket/workflow/document/view Search
    """
    raw_q = (q or "").strip()
    if not raw_q:
        return []

    limit = max(5, min(limit, 50))
    per_type = limit
    search_expr = parse_search_expression(raw_q, field_aliases=_FIELD_ALIASES)
    text_q, field_terms = _parse_search_query(raw_q)
    has_fields = bool(field_terms)
    general_q = text_q if text_q else ("" if has_fields else raw_q)
    advanced_query = bool(search_expr.used_syntax)

    allowed = {t.strip().lower() for t in (type_filter or set()) if t and str(t).strip()}

    def _use(t: str) -> bool:
        return not allowed or t in allowed

    buckets: list[list[dict]] = []
    if advanced_query:
        candidate_limit = max(per_type * 4, 30)
        candidate_limit = min(candidate_limit, 120)
        candidate_queries = extract_positive_search_terms(search_expr, limit=4)
        if not candidate_queries:
            return []

        if _use("matter"):
            buckets.append(
                _merge_bucket_searches(
                    lambda **kwargs: _search_matters(
                        q=kwargs.get("q") or "",
                        limit=kwargs.get("limit") or candidate_limit,
                        field_terms=None,
                        raw_query=kwargs.get("q") or "",
                    ),
                    candidate_queries,
                    limit=candidate_limit,
                )
            )
        if _use("invoice"):
            buckets.append(
                _merge_bucket_searches(_search_invoices, candidate_queries, limit=candidate_limit)
            )
        if _use("client"):
            buckets.append(
                _merge_bucket_searches(
                    lambda **kwargs: _search_clients(
                        q=kwargs.get("q") or "",
                        limit=kwargs.get("limit") or candidate_limit,
                        field_terms=None,
                    ),
                    candidate_queries,
                    limit=candidate_limit,
                )
            )
        if _use("mail"):
            buckets.append(
                _merge_bucket_searches(
                    lambda **kwargs: _search_mail(
                        q=kwargs.get("q") or "",
                        limit=kwargs.get("limit") or candidate_limit,
                        field_terms=None,
                    ),
                    candidate_queries,
                    limit=candidate_limit,
                )
            )
        if _use("docket"):
            buckets.append(
                _merge_bucket_searches(_search_dockets, candidate_queries, limit=candidate_limit)
            )
        if _use("workflow"):
            buckets.append(
                _merge_bucket_searches(_search_workflows, candidate_queries, limit=candidate_limit)
            )
        if _use("document"):
            buckets.append(
                _merge_bucket_searches(_search_documents, candidate_queries, limit=candidate_limit)
            )
        if _use("view"):
            buckets.append(
                _merge_bucket_searches(_search_views, candidate_queries, limit=candidate_limit)
            )

        buckets = [
            [item for item in bucket if _item_matches_expression(item, search_expr)][:per_type]
            for bucket in buckets
        ]
    else:
        if _use("matter"):
            buckets.append(
                _search_matters(
                    q=text_q,
                    limit=per_type,
                    field_terms=field_terms,
                    raw_query=raw_q,
                )
            )
        if _use("invoice"):
            buckets.append(_search_invoices(q=general_q, limit=per_type))
        if _use("client"):
            buckets.append(_search_clients(q=text_q, limit=per_type, field_terms=field_terms))
        if _use("mail"):
            buckets.append(_search_mail(q=text_q, limit=per_type, field_terms=field_terms))
        if _use("docket"):
            buckets.append(_search_dockets(q=general_q, limit=per_type))
        if _use("workflow"):
            buckets.append(_search_workflows(q=general_q, limit=per_type))
        if _use("document"):
            buckets.append(_search_documents(q=general_q, limit=per_type))
        if _use("view"):
            buckets.append(_search_views(q=general_q, limit=per_type))

    out: list[dict] = []
    max_len = max((len(bucket) for bucket in buckets), default=0)
    for i in range(max_len):
        for bucket in buckets:
            if i < len(bucket):
                out.append(_strip_search_meta(bucket[i]))
                if len(out) >= limit:
                    return out
    return out
