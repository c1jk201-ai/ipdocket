from __future__ import annotations

from typing import Any

from app.models.user_saved_view import UserSavedView

_MODULE_PATHS = {
    "case_list": "/case/list",
    "docket_list": "/deadline/list",
    "docket_internal": "/deadline/internal",
    "deadline_calendar_month": "/deadline/calendar/month",
    "invoice_list": "/accounting/invoice-system/invoices",
    "invoice_client_list": "/accounting/invoice-system/clients",
    "crm_client_list": "/crm/",
    "worklog": "/worklog",
    "renewal_fees": "/renewal/fees",
    "renewal_calendar_month": "/renewal/calendar/month",
    "renewal_giveup": "/renewal/giveup",
}


def _safe_view_path(raw_path: str | None, *, module: str) -> str:
    fallback = _MODULE_PATHS.get(module, "/")
    path = (raw_path or "").strip()
    if not path:
        return fallback

    from urllib.parse import urlsplit

    split = urlsplit(path)
    # Only allow relative paths. Disallow absolute URLs (scheme/netloc) and
    # scheme-relative URLs (e.g. //evil.com) to prevent open-redirect vectors.
    if split.scheme or split.netloc:
        return fallback
    if not split.path.startswith("/"):
        return fallback
    if "\\" in split.path:
        return fallback
    return path


def _payload_to_pairs(payload: dict[str, Any]) -> list[tuple[str, str]]:
    filters = payload.get("filters") or payload.get("query") or {}
    out: list[tuple[str, str]] = []
    if isinstance(filters, dict):
        for k, v in filters.items():
            if v is None:
                continue
            if isinstance(v, list):
                out.extend([(str(k), str(x)) for x in v if x is not None and str(x) != ""])
            elif str(v) != "":
                out.append((str(k), str(v)))
    # Standardize top-level payload fields if filters omitted them.
    for key in ("sort", "columns", "per_page"):
        if not isinstance(filters, dict) or key in filters:
            continue
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            out.extend([(str(key), str(x)) for x in value if x is not None and str(x) != ""])
        elif str(value) != "":
            out.append((str(key), str(value)))
    return out


def _build_view_url_from_payload(*, view_id: str, module: str, payload: dict[str, Any]) -> str:
    path = _safe_view_path(payload.get("path"), module=str(module or ""))
    pairs = _payload_to_pairs(payload)
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    split = urlsplit(path)
    existing_pairs = parse_qsl(split.query, keep_blank_values=True)
    has_view_id = any(k == "view_id" for k, _ in pairs) or any(
        k == "view_id" for k, _ in existing_pairs
    )
    if not has_view_id:
        pairs.append(("view_id", str(view_id)))
    all_pairs = existing_pairs + pairs
    if not all_pairs:
        return path
    qs = urlencode(all_pairs, doseq=True)
    return urlunsplit((split.scheme, split.netloc, split.path, qs, split.fragment))


def build_view_url(view: UserSavedView) -> str:
    payload = view.payload_json or {}
    return _build_view_url_from_payload(
        view_id=str(view.id),
        module=str(view.module or ""),
        payload=payload,
    )


def serialize_view(view: UserSavedView, *, include_payload: bool = True) -> dict[str, Any]:
    payload = view.payload_json or {}
    url = build_view_url(view)
    return {
        "id": view.id,
        "name": view.name,
        "scope": view.scope,
        "module": view.module,
        "is_default": bool(view.is_default),
        "payload": payload if include_payload else None,
        "url": url,
    }


_SYSTEM_DEFAULT_ROLES = {
    "admin",
    "lead_attorney",
    "partner_attorney",
    "mgmt_director",
}

_CASE_OPERATOR_ROLES = {
    "attorney",
    "handler",
    "manager",
    "staff",
    "patent_staff",
    "patent_engineer",
    "paralegal",
    "mgmt_staff",
    "accounting",
}

_INVOICE_OPERATOR_ROLES = {
    "accounting",
    "manager",
    "mgmt_staff",
    "mgmt_director",
}


def _role_names(user: Any) -> set[str]:
    if not user:
        return set()
    try:
        names = getattr(user, "role_names", None)
    except Exception:
        names = None
    if isinstance(names, (set, frozenset, list, tuple)):
        return {str(v or "").strip().lower() for v in names if str(v or "").strip()}

    out = set()
    raw = getattr(user, "role", None) or getattr(user, "user_role", None) or ""
    for part in str(raw or "").split(","):
        value = part.strip().lower()
        if value:
            out.add(value)
    return out


def _has_staff_identity(user: Any) -> bool:
    return bool(str(getattr(user, "staff_party_id", "") or "").strip())


def _system_payload(path: str, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "path": path,
        "filters": dict(filters or {}),
    }


def _system_view(
    module: str,
    key: str,
    name: str,
    payload: dict[str, Any],
    *,
    is_default: bool = False,
) -> dict[str, Any]:
    view_id = f"system:{module}:{key}"
    return {
        "id": view_id,
        "name": name,
        "scope": "system",
        "module": module,
        "is_default": bool(is_default),
        "payload": payload,
        "url": _build_view_url_from_payload(view_id=view_id, module=module, payload=payload),
    }


def system_saved_views_for_user(
    module: str,
    user: Any,
    *,
    allow_system_default: bool = True,
) -> list[dict[str, Any]]:
    """Return role-aware built-in saved views for hot list screens.

    These are not persisted rows. They appear beside user/team views so teams get a
    sensible baseline without seeding per-user data.
    """

    module = str(module or "").strip()
    roles = _role_names(user)
    is_super_case_role = bool(roles.intersection(_SYSTEM_DEFAULT_ROLES))
    is_case_operator = _has_staff_identity(user) and (
        bool(roles.intersection(_CASE_OPERATOR_ROLES)) or not roles
    )
    is_invoice_operator = bool(roles.intersection(_INVOICE_OPERATOR_ROLES))

    if module == "case_list":
        return [
            _system_view(
                module,
                "my_cases",
                " Matter",
                _system_payload("/case/list", {"assigned": "me", "per_page": "50"}),
                is_default=allow_system_default and is_case_operator and not is_super_case_role,
            ),
            _system_view(
                module,
                "due7",
                " Deadline",
                _system_payload("/case/list", {"due": "due7", "per_page": "50"}),
            ),
            _system_view(
                module,
                "overdue",
                " Deadline",
                _system_payload("/case/list", {"due": "overdue", "per_page": "50"}),
            ),
        ]

    if module in {"docket_list", "docket_internal"}:
        path = "/deadline/internal" if module == "docket_internal" else "/deadline/list"
        return [
            _system_view(
                module,
                "todo",
                "In progress",
                _system_payload(path, {"filter": "todo", "per_page": "50"}),
            ),
            _system_view(
                module,
                "due7",
                " Deadline",
                _system_payload(path, {"filter": "due7", "per_page": "50"}),
            ),
            _system_view(
                module,
                "overdue",
                " Deadline",
                _system_payload(path, {"filter": "overdue", "per_page": "50"}),
            ),
        ]

    if module == "invoice_list":
        return [
            _system_view(
                module,
                "outstanding",
                "outstanding ",
                _system_payload(
                    "/accounting/invoice-system/invoices",
                    {"status": "sent_unpaid_or_pending", "sort": "issue_date_desc"},
                ),
                is_default=allow_system_default and is_invoice_operator,
            ),
            _system_view(
                module,
                "paid_no_tax",
                "Paid - ",
                _system_payload(
                    "/accounting/invoice-system/invoices",
                    {"status": "paid_no_tax", "sort": "issue_date_desc"},
                ),
            ),
            _system_view(
                module,
                "drafts",
                "Draft",
                _system_payload(
                    "/accounting/invoice-system/invoices",
                    {"status": "draft", "sort": "issue_date_desc"},
                ),
            ),
        ]

    if module == "invoice_client_list":
        return [
            _system_view(
                module,
                "outstanding",
                "outstanding Client",
                _system_payload(
                    "/accounting/invoice-system/clients",
                    {"has_outstanding": "1", "sort": "last_invoice", "per_page": "50"},
                ),
                is_default=allow_system_default and is_invoice_operator,
            ),
            _system_view(
                module,
                "revenue",
                "Revenue",
                _system_payload(
                    "/accounting/invoice-system/clients",
                    {"sort": "revenue", "per_page": "50"},
                ),
            ),
            _system_view(
                module,
                "recent_invoice",
                "Recent Invoice",
                _system_payload(
                    "/accounting/invoice-system/clients",
                    {"sort": "last_invoice", "per_page": "50"},
                ),
            ),
        ]

    if module == "crm_client_list":
        return [
            _system_view(
                module,
                "recent",
                "Recent Registration",
                _system_payload("/crm/", {"sort": "id", "direction": "desc", "per_page": "20"}),
                is_default=allow_system_default,
            ),
            _system_view(
                module,
                "name",
                "Name",
                _system_payload("/crm/", {"sort": "name", "direction": "asc", "per_page": "50"}),
            ),
            _system_view(
                module,
                "invoice_missing",
                "Invoice Link",
                _system_payload(
                    "/crm/",
                    {"invoice_link": "missing", "sort": "id", "direction": "desc"},
                ),
            ),
        ]

    return []
