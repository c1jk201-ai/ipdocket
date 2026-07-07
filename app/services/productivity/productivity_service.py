"""
Refactored Productivity Service.
This file now acts as a facade, re-exporting functionality from split services.
"""

from __future__ import annotations

from typing import Any, Optional

from app.services.productivity.quick_action_service import (
    _classify_doc,
    _extract_text,
    _pick_due_date,
    apply_doc_suggestions,
    doc_suggest_from_upload,
    quick_add_docket,
    quick_add_invoice,
    quick_add_workflow,
    undo_by_token,
)
from app.services.productivity.reminder_service import (
    _effective_due_expr,
    _effective_due_value,
    _event_key_map_available,
    _is_statutory,
    _matter_event_available,
    _not_done_filter,
    ensure_docket_reminders,
    get_today_todos,
)
from app.services.productivity.search_service import (
    _apply_docket_visibility_filter,
    _build_subtitle,
    _can_access_folder,
    _can_view_all_workflows,
    _case_index_available,
    _ilike,
    _looks_like_identifier,
    _mail_table_available,
    _normalize_identifier,
    _normalized_field,
    _parse_search_query,
    _search_clients,
    _search_dockets,
    _search_documents,
    _search_invoices,
    _search_mail,
    _search_matters,
    _search_views,
    _search_workflows,
    quick_search,
)
from app.services.productivity.utils import check_can_access_matter_id as _can_access_matter_id
from app.services.productivity.utils import get_docket_pk as _docket_pk
from app.services.productivity.utils import get_docket_title as _docket_title
from app.services.productivity.utils import get_today as _today
from app.services.productivity.utils import get_user_id as _user_id
from app.services.productivity.utils import has_attr_safe as _has_attr
from app.services.productivity.utils import set_if_attr as _set_if_attr


def _docket_int_id(d: Any) -> Optional[int]:
    raw = _docket_pk(d)
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
