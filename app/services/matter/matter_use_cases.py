from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any, List, Mapping, Optional

from flask import current_app
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from app.blueprints.case.helpers import _sync_matter_events_from_dom_patent
from app.extensions import db
from app.models.case import Case
from app.models.deadline import Deadline, RenewalFee
from app.models.ip_records import (
    Family,
    Matter,
    MatterCustomField,
    MatterEvent,
    MatterFamily,
    MatterIdentifier,
    VMatterOverview,
)
from app.models.user import User
from app.models.workflow import Workflow
from app.models.worklog import WorkLog
from app.services.case.canonical_field_service import upsert_case_flat_index
from app.services.case.case_kind import PATENT_LIKE_TYPES, resolve_public_case_kind
from app.services.case.case_parameter_service import CaseParameterService, CaseProfile
from app.services.case.form_support import get_case_date as _get_case_date
from app.services.case.form_support import is_yes as _is_yes
from app.services.case.form_support import log_custom_field_filtering as _log_custom_field_filtering
from app.services.case.form_support import normalize_date_input as _normalize_date_input
from app.services.case.form_support import normalize_our_ref_input as _normalize_our_ref_input
from app.services.case.form_support import parse_int as _parse_int
from app.services.case.form_support import (
    validate_custom_field_updates as _validate_custom_field_updates,
)
from app.services.case.form_support import validate_application_number
from app.services.case.helpers_files import _attach_image_file_asset, _is_allowed_image_upload
from app.services.case.helpers_staff import _BASIC_CANONICAL_STAFF_KEYS, _update_basic_matter_info
from app.services.case.staff_context import (
    build_staff_assignment_context as _build_staff_assignment_context,
)
from app.services.case.staff_context import (
    build_staff_picker_context as _build_staff_picker_context,
)
from app.services.deadlines.docket_service import (
    complete_exam_request_docket,
    complete_filing_docket,
    complete_registration_docket,
    upsert_exam_request_docket,
    upsert_filing_docket,
    upsert_registration_docket,
)
from app.services.matter.auto_status_apply import (
    apply_auto_status_from_db as _apply_auto_status_from_db,
)
from app.services.matter.matter_domain import (
    DomesticPatentUpdateCommand,
    DomesticPatentUpdateResult,
    MatterCreateCommand,
    MatterCreatePrepareCommand,
    MatterCreatePrepareResult,
    MatterCreateResult,
)
from app.services.matter.matter_family_service import (
    link_matter_to_family_id,
    link_matters_into_family,
)
from app.utils.error_logging import report_swallowed_exception


class _SimpleDateCalculator:
    def calculate_date(self, base_date: date, *, years: int = 0, days: int = 0):
        try:
            return base_date.replace(year=base_date.year + int(years)) + timedelta(days=int(days))
        except ValueError:
            return base_date.replace(month=2, day=28, year=base_date.year + int(years)) + timedelta(
                days=int(days)
            )


def _infer_application_country_for_create(
    form_data: dict[str, Any],
    *,
    division: str,
    matter_type: str,
) -> None:
    country = (form_data.get("application_country") or "").strip().upper()
    if not country:
        ref = _normalize_our_ref_input(form_data.get("our_ref"))
        match = re.search(r"\d{4}(?P<country>[A-Z]{2,3})$", ref or "", re.IGNORECASE)
        if match:
            country = match.group("country").upper()
        elif (matter_type or "").strip().upper() == "PCT":
            country = "PCT"
        elif (division or "").strip().upper() in {"DOM", "INC"}:
            country = "US"

    if country:
        form_data["application_country"] = country


def _sync_dockets_from_form(matter_id: str, form: Mapping[str, Any], commit: bool = False) -> None:
    """Create/complete docket formfrom  Process (Duplicate )."""
    touched_refs: list[str] = []
    if (form.get("filing_deadline") or "").strip():
        upsert_filing_docket(
            matter_id,
            form.get("filing_deadline"),
            deadline_type=form.get("filing_deadline_type"),
            commit=commit,
        )
        touched_refs.extend(["Filing", "Filing (Process)", "MGMT:FILING"])
    if (form.get("exam_deadline") or "").strip():
        upsert_exam_request_docket(matter_id, form.get("exam_deadline"), commit=commit)
        touched_refs.extend(["Examination request", "Examination request (Process)", "MGMT:EXAM_REQUEST"])
    if (form.get("reg_deadline") or "").strip():
        upsert_registration_docket(matter_id, form.get("reg_deadline"), commit=commit)
        touched_refs.extend(["Registration", "Registration (Process)", "MGMT:REGISTRATION"])

    if (form.get("application_date") or "").strip():
        complete_filing_docket(matter_id, form.get("application_date"), commit=commit)
        touched_refs.extend(["Filing", "Filing (Process)", "MGMT:FILING", "MGMT:STATUS_RED:FilingDeadline"])
    if (form.get("exam_request_date") or "").strip():
        complete_exam_request_docket(matter_id, form.get("exam_request_date"), commit=commit)
        touched_refs.extend(
            ["Examination request", "Examination request (Process)", "MGMT:EXAM_REQUEST", "MGMT:STATUS_RED:Examination requestDeadline"]
        )
    if (form.get("registration_date") or "").strip():
        complete_registration_docket(matter_id, form.get("registration_date"), commit=commit)
        touched_refs.extend(
            ["Registration", "Registration (Process)", "MGMT:REGISTRATION", "MGMT:STATUS_RED:RegistrationDeadline"]
        )

    _sync_touched_dockets_now(matter_id, touched_refs)
    if commit and touched_refs:
        db.session.commit()


def _sync_touched_dockets_now(matter_id: str, refs: list[str]) -> None:
    """Make newly-created form dockets visible as workflows without waiting for queue drain."""
    clean_refs = [ref for ref in dict.fromkeys(refs) if ref]
    if not matter_id or not clean_refs:
        return
    try:
        from app.models.ip_records import DocketItem
        from app.services.workflow.task_sync import sync_from_docket_item

        rows = (
            DocketItem.query.filter(DocketItem.matter_id == str(matter_id))
            .filter(DocketItem.name_ref.in_(clean_refs))
            .filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
            .order_by(DocketItem.name_ref.asc(), DocketItem.docket_id.asc())
            .all()
        )
        rank = {ref: idx for idx, ref in enumerate(clean_refs)}
        for docket_item in sorted(
            rows,
            key=lambda item: (
                rank.get((getattr(item, "name_ref", None) or "").strip(), 999),
                (getattr(item, "name_ref", None) or "").strip(),
                (getattr(item, "docket_id", None) or "").strip(),
            ),
        ):
            sync_from_docket_item(docket_item=docket_item, actor_id=None)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_use_cases._sync_touched_dockets_now",
            log_key="matter_use_cases._sync_touched_dockets_now",
            log_window_seconds=300,
        )


def _parse_event_date_token(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    token = str(value or "").strip()
    if not token:
        return None
    token = token.split("T", 1)[0].split(" ", 1)[0].strip()
    if not token:
        return None
    try:
        return date.fromisoformat(token)
    except Exception:
        return None


def _intake_completion_date_from_events(matter_id: str) -> date | None:
    mid = str(matter_id or "").strip()
    if not mid:
        return None

    key_groups: tuple[tuple[str, ...], ...] = (
        ("Filing date", "APPLICATION_DATE"),
        ("ForeignFiling date", "FOREIGN_APPLICATION_DATE"),
    )
    target_keys = {key for keys in key_groups for key in keys}

    try:
        rows = (
            db.session.query(MatterEvent.event_key, MatterEvent.event_at)
            .filter(MatterEvent.matter_id == mid, MatterEvent.event_at.isnot(None))
            .all()
        )
    except Exception:
        current_app.logger.exception(
            "Failed to lookup intake completion signals for matter_id=%s", mid
        )
        return None

    latest_by_key: dict[str, date] = {}
    for event_key, event_at in rows:
        key = str(event_key or "").strip()
        if key not in target_keys:
            continue
        parsed = _parse_event_date_token(event_at)
        if not parsed:
            continue
        prev = latest_by_key.get(key)
        if not prev or parsed > prev:
            latest_by_key[key] = parsed

    for keys in key_groups:
        candidates = [latest_by_key[k] for k in keys if k in latest_by_key]
        if candidates:
            return max(candidates)
    return None


def _create_intake_workflows(*, matter_id: str, actor_id: int | None) -> int:
    if not matter_id:
        return 0

    base_code = f"INTAKE:{matter_id}"
    intake_completed_on = _intake_completion_date_from_events(str(matter_id))
    initial_status = "Completed" if intake_completed_on else "Pending"

    try:
        existing_rows = Workflow.query.filter(Workflow.business_code.like(f"{base_code}%")).all()
        if existing_rows:
            if intake_completed_on:
                for wf in existing_rows:
                    if (wf.status or "").strip() in ("Completed", "Abandoned"):
                        continue
                    wf.status = "Completed"
                    wf.completed_date = intake_completed_on
                    db.session.add(wf)
            return 0
    except Exception:
        existing_rows = []

    try:
        from app.utils.task_assignment_rules import AssigneeInfo, resolve_assignees_for_task

        assignees = resolve_assignees_for_task(
            matter_id=str(matter_id),
            name_ref="INTAKE",
            name_free=" Confirm",
            category="MGMT",
            fallback_user_id=actor_id,
            source=None,
        )
    except Exception:
        assignees = []

    if not assignees and actor_id:
        try:
            assignees = [AssigneeInfo(int(actor_id), "fallback", None)]
        except Exception:
            assignees = []

    today = date.today()
    created = 0

    if not assignees:
        business_code = f"{base_code}:unassigned"
        if not Workflow.query.filter_by(business_code=business_code).first():
            wf = Workflow(
                case_id=str(matter_id),
                name=" Confirm",
                category="MGMT",
                due_date=today,
                status=initial_status,
                completed_date=intake_completed_on,
                assignee_id=None,
                created_by_id=actor_id,
                business_code=business_code,
                note=" Wizard Auto Create",
            )
            db.session.add(wf)
            created += 1
        return created

    for info in assignees:
        user_id = info.user_id
        business_code = f"{base_code}:{user_id}"
        if Workflow.query.filter_by(business_code=business_code).first():
            continue
        wf = Workflow(
            case_id=str(matter_id),
            name=" Confirm",
            category="MGMT",
            due_date=today,
            status=initial_status,
            completed_date=intake_completed_on,
            assignee_id=user_id,
            created_by_id=actor_id,
            business_code=business_code,
            note=" Wizard Auto Create",
        )
        db.session.add(wf)
        created += 1

    return created


def _is_matter_our_ref_integrity_error(exc: IntegrityError) -> bool:
    msg = str(getattr(exc, "orig", exc) or "").lower()
    if not msg:
        return False
    if "duplicate" not in msg and "unique" not in msg:
        return False
    return any(
        token in msg
        for token in (
            "ix_matter_our_ref",
            "ux_matter_our_ref",
            "matter_our_ref",
            "key (our_ref)",
            "(our_ref)=",
        )
    )


def _release_deleted_matter_our_ref(*, row: Matter, requested_ref: str) -> None:
    """Release Our Ref held by a soft-deleted matter so it can be reused."""
    base = f"{requested_ref}#deleted-{str(row.matter_id or '')[:8]}"
    candidate = base
    seq = 1
    while Matter.query.filter_by(our_ref=candidate).first():
        candidate = f"{base}-{seq}"
        seq += 1

    prev_old_ref = (row.old_our_ref or "").strip()
    if requested_ref and requested_ref != prev_old_ref:
        if prev_old_ref:
            row.old_our_ref = f"{prev_old_ref} | {requested_ref}"
        else:
            row.old_our_ref = requested_ref

    row.our_ref = candidate
    db.session.flush()


class SessionIdempotencyStore:
    def __init__(self, session_obj):
        self.s = session_obj
        self.CTX_KEY = "matter_create_contexts"
        self.USED_KEY = "matter_create_used_keys"
        self.RES_KEY = "matter_create_results"

    def _trim(self, val: dict, limit: int = 20) -> dict:
        if not isinstance(val, dict):
            return {}
        if len(val) <= limit:
            return val
        items = list(val.items())[-limit:]
        return dict(items)

    def register_context(self, div: str, typ: str) -> str:
        key = uuid.uuid4().hex
        contexts = self.s.get(self.CTX_KEY) or {}
        contexts[str(key)] = {"division": div or "", "type": typ or ""}
        try:
            self.s[self.CTX_KEY] = self._trim(contexts, limit=20)
            self.s.modified = True
            current_app.logger.debug(
                f"SessionIdempotencyStore: Registered context key={key} div={div} type={typ}. Session modified marked."
            )
        except Exception:
            current_app.logger.exception("SessionIdempotencyStore: Failed to save context")
        return key

    def load_context(self, key: str) -> tuple[str, str]:
        contexts = self.s.get(self.CTX_KEY) or {}
        ctx = contexts.get(str(key))
        current_app.logger.debug(
            f"SessionIdempotencyStore: Loading context key={key}. Found: {bool(ctx)}"
        )
        if not ctx:
            return "", ""
        return (ctx.get("division") or ""), (ctx.get("type") or "")

    def check_used(self, key: str) -> tuple[bool, str | None]:
        used = self.s.get(self.USED_KEY) or []
        if str(key) in used:
            results = self.s.get(self.RES_KEY) or {}
            current_app.logger.debug(f"SessionIdempotencyStore: Key {key} already used.")
            return True, results.get(str(key))
        return False, None

    def mark_used(self, key: str, matter_id: str) -> None:
        used = list(dict.fromkeys(self.s.get(self.USED_KEY) or []))
        skey = str(key)
        if skey not in used:
            used.append(skey)
        if len(used) > 50:
            used = used[-50:]
        results = self.s.get(self.RES_KEY) or {}
        results[skey] = str(matter_id)

        self.s[self.USED_KEY] = used
        self.s[self.RES_KEY] = self._trim(results, limit=50)

        contexts = self.s.get(self.CTX_KEY) or {}
        if skey in contexts:
            contexts.pop(skey, None)
            self.s[self.CTX_KEY] = contexts

        self.s.modified = True
        current_app.logger.debug(
            f"SessionIdempotencyStore: Marked key {key} as used for matter {matter_id}"
        )


class DomesticPatentUpdateUseCase:
    def execute(self, cmd: DomesticPatentUpdateCommand) -> DomesticPatentUpdateResult:
        matter_id = cmd.matter_id
        allowed_keys = CaseParameterService.get_allowed_keys("DOM", "PATENT")
        row = MatterCustomField.query.filter_by(
            matter_id=matter_id, namespace="domestic_patent"
        ).first()
        if not row:
            row = MatterCustomField(matter_id=matter_id, namespace="domestic_patent", data={})
            db.session.add(row)
        data = dict(row.data or {})
        for sk in _BASIC_CANONICAL_STAFF_KEYS:
            data.pop(sk, None)
        _log_custom_field_filtering(
            matter_id=str(matter_id),
            namespace="domestic_patent",
            form_data=cmd.form_data,
            allowed_keys=allowed_keys,
        )
        updates = _validate_custom_field_updates(
            matter_id=str(matter_id),
            namespace="domestic_patent",
            form_data=cmd.form_data,
            allowed_keys=allowed_keys,
            strict_dates=bool(current_app.config.get("CASE_STRICT_DATE_VALIDATION", True)),
        )
        data.update(updates)
        row.data = data
        db.session.flush()
        upsert_case_flat_index(matter_id)
        with db.session.begin_nested():
            _sync_matter_events_from_dom_patent(matter_id=matter_id, dom_patent=data)
            matter = Matter.query.get(matter_id)
            _apply_auto_status_from_db(matter=matter, dom_patent=data)
        form = cmd.form_data
        _sync_dockets_from_form(matter_id, form, commit=False)
        try:
            from app.services.deadlines.mgmt_deadlines import ensure_mgmt_deadlines_for_matter

            ensure_mgmt_deadlines_for_matter(str(matter_id), commit=False)
            matter = Matter.query.get(matter_id)
            if matter is not None:
                _apply_auto_status_from_db(matter=matter, dom_patent=data)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="matter_use_cases.DomesticPatentUpdateUseCase.ensure_mgmt_deadlines_for_matter",
                log_key="matter_use_cases.DomesticPatentUpdateUseCase.ensure_mgmt_deadlines_for_matter",
                log_window_seconds=300,
            )
        return DomesticPatentUpdateResult(updated=True, warnings=[], dockets_touched=0)


class MatterCreatePrepareUseCase:
    def execute(
        self, cmd: MatterCreatePrepareCommand, store: SessionIdempotencyStore
    ) -> MatterCreatePrepareResult:
        div = cmd.division
        typ = cmd.case_type
        idempotency_key = store.register_context(div, typ)
        is_dom_pat = div == "DOM" and typ in PATENT_LIKE_TYPES
        # ... logic ...
        field_layout, field_meta = CaseParameterService.get_field_layout_with_meta(div, typ)
        staff_picker = _build_staff_picker_context()
        staff_assignment = _build_staff_assignment_context()

        # Defaults
        from dateutil.relativedelta import relativedelta

        today = date.today()
        prefill = {
            "retained_at": today.isoformat(),
            "entered_at": today.isoformat(),
            "filing_deadline": (today + relativedelta(months=1)).isoformat(),
            "filing_deadline_type": "INTERNAL",
        }
        allowed_prefill = CaseParameterService.get_allowed_keys(div, typ)
        for raw_key, raw_value in (cmd.raw_args or {}).items():
            prefill_key = (raw_key or "").strip()
            if not prefill_key or prefill_key not in allowed_prefill:
                continue
            value = raw_value
            if isinstance(value, (list, tuple)):
                value = value[0] if value else ""
            if value is None:
                continue
            value = str(value).strip()
            if value:
                prefill[prefill_key] = value

        for key, info in (field_meta or {}).items():
            if key not in allowed_prefill:
                continue
            if str(prefill.get(key) or "").strip():
                continue
            default_value = info.get("default_value") if isinstance(info, dict) else None
            if default_value in (None, ""):
                continue
            prefill[key] = str(default_value)

        # OUT defaults (Examination request  )
        from app.services.deadlines.exam_request_rules import (
            apply_exam_request_date_default_when_requested,
            apply_out_exam_request_defaults,
        )

        apply_out_exam_request_defaults(
            prefill,
            division=div,
            case_type=typ,
            allowed_keys=set(allowed_prefill),
        )
        apply_exam_request_date_default_when_requested(
            prefill,
            allowed_keys=set(allowed_prefill),
        )

        return MatterCreatePrepareResult(
            div,
            typ,
            field_layout,
            field_meta,
            idempotency_key,
            prefill,
            {
                "staff_picker": staff_picker,
                "staff_assignment": staff_assignment,
            },
        )


class MatterCreateApplyUseCase:
    def execute(
        self, cmd: MatterCreateCommand, store: SessionIdempotencyStore
    ) -> MatterCreateResult:
        used, existing_id = store.check_used(cmd.idempotency_key)
        if used:
            return MatterCreateResult(
                success=True,
                matter_id=existing_id,
                existing_id=existing_id,
                redirect_to_list=(existing_id is None),
            )

        form_data = dict(cmd.form_data)
        division = cmd.division
        matter_type = cmd.case_type
        image_file = cmd.files.get("image_file") if cmd.files else None
        actor_id = cmd.actor_user_id
        actor = User.query.get(actor_id) if actor_id else None

        # Business default: auto 30-day filing deadline is treated as internal unless explicitly set.
        if (form_data.get("filing_deadline") or "").strip() and not (
            form_data.get("filing_deadline_type") or ""
        ).strip():
            form_data["filing_deadline_type"] = "INTERNAL"

        try:
            profile = CaseParameterService.get_case_profile(division, matter_type)
        except ValueError as e:
            return MatterCreateResult(False, error=str(e))

        _infer_application_country_for_create(
            form_data,
            division=division,
            matter_type=matter_type,
        )

        # OUT defaults/constraints (Examination request , US Billing  )
        from app.services.deadlines.exam_request_rules import (
            apply_exam_request_date_default_when_requested,
            apply_out_exam_request_defaults,
        )

        apply_out_exam_request_defaults(
            form_data,
            division=division,
            case_type=matter_type,
            allowed_keys=set(profile.allowed_keys),
        )
        apply_exam_request_date_default_when_requested(
            form_data,
            allowed_keys=set(profile.allowed_keys),
        )

        # Image check
        # Image check
        if profile.supports_image and image_file and (image_file.filename or "").strip():
            if not _is_allowed_image_upload(image_file):
                return MatterCreateResult(
                    False, error="/Image Only image files can be uploaded."
                )

        # Required fields check
        missing_fields = CaseParameterService.validate_required_fields(
            form_data, division, matter_type
        )
        if missing_fields:
            return MatterCreateResult(False, validation_errors=missing_fields)

        retained_at = _normalize_date_input(form_data.get("retained_at"), "Engagement date")
        if not retained_at:
            return MatterCreateResult(
                False, validation_errors=[{"key": "retained_at", "label": "Engagement date"}]
            )
        form_data["retained_at"] = retained_at

        # Client check
        if (form_data.get("client_name") or "").strip() and not (
            form_data.get("client_id") or ""
        ).strip():
            return MatterCreateResult(
                False, validation_errors=[{"key": "client_name", "label": "Client"}]
            )

        # Application number check
        if division in ("DOM", "INC"):
            app_no = (form_data.get("application_no") or "").strip()
            if app_no and not validate_application_number(app_no):
                pass  # Original verified warning but didn't blockNew logic said "flash warning" but continued.

        strict_dates = bool(current_app.config.get("CASE_STRICT_DATE_VALIDATION", True))
        try:
            _validate_custom_field_updates(
                matter_id="",
                namespace=profile.namespace,
                form_data=form_data,
                allowed_keys=profile.allowed_keys,
                strict_dates=strict_dates,
            )
        except ValueError as e:
            return MatterCreateResult(False, error=str(e))

        # Priority check
        # ... logic ...

        our_ref = _normalize_our_ref_input(form_data.get("our_ref"))
        if not our_ref:
            return MatterCreateResult(
                False, validation_errors=[{"key": "our_ref", "label": "Our Ref."}]
            )

        existing = Matter.query.filter_by(our_ref=our_ref).first()
        if existing:
            if bool(getattr(existing, "is_deleted", False)):
                _release_deleted_matter_our_ref(row=existing, requested_ref=our_ref)
            else:
                return MatterCreateResult(False, error="  Our Ref. .")

        app_route = (form_data.get("_forced_app_route") or form_data.get("app_route") or "").strip()
        app_route_lower = app_route.lower()
        right_type = (form_data.get("right_type") or "").strip().lower()
        case_kind_value = (form_data.get("case_kind") or "").strip().lower()
        is_madrid_route = "madrid" in app_route_lower or "\ub9c8\ub4dc\ub9ac\ub4dc" in app_route
        is_hague_route = "hague" in app_route_lower or "\ud5e4\uc774\uadf8" in app_route
        is_copyright_kind = (
            "copyright" in right_type
            or "\uc800\uc791\uad8c" in right_type
            or "copyright" in case_kind_value
            or "\uc800\uc791\uad8c" in case_kind_value
        )
        storage_division, storage_type = resolve_public_case_kind(
            division,
            matter_type,
            is_madrid=bool(matter_type == "MADRID" or is_madrid_route),
            is_hague=bool(matter_type == "HAGUE" or is_hague_route),
            is_copyright=bool(matter_type == "COPYRIGHT" or is_copyright_kind),
        )
        if not storage_type:
            try:
                from app.services.case.case_menu_config import (
                    find_case_menu_item,
                    normalize_case_menu_division,
                    normalize_case_menu_type,
                )

                menu_item = find_case_menu_item(division, matter_type)
                if menu_item:
                    storage_division = normalize_case_menu_division(division) or str(
                        menu_item.get("division") or ""
                    )
                    storage_type = normalize_case_menu_type(matter_type) or str(
                        menu_item.get("type") or ""
                    )
            except Exception:
                current_app.logger.debug("case menu storage fallback lookup failed", exc_info=True)

        m = Matter(
            our_ref=our_ref,
            old_our_ref=(form_data.get("old_our_ref") or "").strip() or None,
            your_ref=(form_data.get("your_ref") or "").strip() or None,
            right_group=storage_division or None,
            matter_type=storage_type or None,
            inhouse_status=(form_data.get("inhouse_status") or "").strip() or None,
            memo=(form_data.get("memo") or "").strip() or None,
            retained_at=retained_at,
            entered_at=_normalize_date_input(form_data.get("entered_at"), "")
            or date.today().isoformat(),
        )
        db.session.add(m)
        try:
            with db.session.begin_nested():
                db.session.flush()
        except IntegrityError as exc:
            if _is_matter_our_ref_integrity_error(exc):
                return MatterCreateResult(False, error="  Our Ref. .")
            raise

        # Sync helpers
        def _process_profile(p: CaseProfile) -> None:
            ns = p.namespace
            row = MatterCustomField.query.filter_by(
                matter_id=str(m.matter_id), namespace=ns
            ).first()
            if not row:
                row = MatterCustomField(matter_id=str(m.matter_id), namespace=ns, data={})
                db.session.add(row)
            data = dict(row.data or {})
            _log_custom_field_filtering(
                matter_id=str(m.matter_id),
                namespace=ns,
                form_data=form_data,
                allowed_keys=p.allowed_keys,
            )
            updates = _validate_custom_field_updates(
                matter_id=str(m.matter_id),
                namespace=ns,
                form_data=form_data,
                allowed_keys=p.allowed_keys,
                strict_dates=strict_dates,
            )
            data.update(updates)
            # same client
            if _is_yes(form_data.get("applicant_same_as_client")):
                data["applicant_name"] = (form_data.get("client_name") or "").strip()
            # proposal title legacy
            rn = (form_data.get("right_name") or "").strip()
            if not data.get("proposal_title") and rn:
                data["proposal_title"] = rn

            # Attach image if design/tm
            if p.supports_image and image_file:
                try:
                    _attach_image_file_asset(matter_id=str(m.matter_id), file=image_file, data=data)
                except ValueError as e:
                    # usecase from 500to   User Error 
                    raise ValueError(str(e))

            row.data = data
            kwargs = {p.arg_key: data}
            if p.id_sync:
                with db.session.begin_nested():
                    p.id_sync(matter_id=str(m.matter_id), **kwargs)
            if p.ev_sync:
                with db.session.begin_nested():
                    p.ev_sync(matter_id=str(m.matter_id), **kwargs)
            if p.auto_status:
                with db.session.begin_nested():
                    _apply_auto_status_from_db(matter=m, **kwargs)

        m.right_name = (form_data.get("right_name") or "").strip() or None

        try:
            _process_profile(profile)
        except ValueError as e:
            return MatterCreateResult(False, error=str(e))

        # Persist staff selections (manager/attorney/handler) to basic namespace and assignments.
        _update_basic_matter_info(str(m.matter_id), form_data)

        try:
            from app.services.deadlines.mgmt_deadlines import ensure_mgmt_deadlines_for_matter

            ensure_mgmt_deadlines_for_matter(str(m.matter_id), commit=False)
            _apply_auto_status_from_db(matter=m)
        except Exception:
            current_app.logger.exception("ensure_mgmt_deadlines_for_matter failed")

        try:
            _create_intake_workflows(matter_id=str(m.matter_id), actor_id=actor_id)
        except Exception:
            current_app.logger.exception("intake workflow creation failed")

        # Apply same client logic if requested (helper usage)
        # Note: _apply_same_client_logic_helper is for PRE-fill (from context)
        # Here we might need to update Client (if implemented) or just trust input.

        # Family Linking
        family_target_id = (form_data.get("family_link_target_id") or "").strip()
        family_id = (form_data.get("param_family_id") or "").strip()
        try:
            if family_target_id:
                target = Matter.query.get(family_target_id)
                if target:
                    link_matters_into_family(
                        primary_matter=m,
                        target_matter=target,
                        prefer_primary=True,
                        link_role="manual",
                        actor=actor,
                    )
            elif family_id:
                link_matter_to_family_id(
                    matter=m,
                    family_id=family_id,
                    link_role="manual",
                    actor=actor,
                )
        except PermissionError:
            current_app.logger.warning(
                "Family linking denied during create (matter_id=%s, target_id=%s, family_id=%s)",
                str(m.matter_id),
                family_target_id,
                family_id,
            )
        except Exception:
            current_app.logger.exception(
                "Family linking failed during create (matter_id=%s, target_id=%s, family_id=%s)",
                str(m.matter_id),
                family_target_id,
                family_id,
            )

        # Initial Worklog (Created)
        log = WorkLog(
            matter_id=str(m.matter_id),
            completed_by_id=actor_id,
            action_type="create",
            description=f"Matter Create ({storage_division}/{storage_type}) - Ref: {m.our_ref}",
        )
        db.session.add(log)

        upsert_case_flat_index(str(m.matter_id))

        try:
            _sync_dockets_from_form(str(m.matter_id), form_data, commit=False)
        except Exception:
            current_app.logger.exception("initial docket sync failed")

        store.mark_used(cmd.idempotency_key, str(m.matter_id))
        return MatterCreateResult(True, matter_id=str(m.matter_id))
