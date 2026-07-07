from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import String, and_, case, cast, func, inspect, literal, or_
from sqlalchemy.exc import OperationalError, ProgrammingError

from app import db
from app.models.case import Case
from app.models.deadline import Deadline, RenewalFee
from app.models.invoice import Invoice
from app.models.parse_failure import ParseFailure
from app.models.ip_records import AnnuityItem, DocketItem, Matter, MatterStaffAssignment
from app.models.workflow import Workflow
from app.services.ops.data_quality import get_dummy_candidates
from app.utils.error_logging import report_swallowed_exception


def _is_blank_text(col):
    return or_(col.is_(None), func.trim(func.coalesce(col, "")) == "")


def _legacy_case_matter_join_expr():
    return or_(
        Matter.our_ref == Case.ref_no,
        Matter.old_our_ref == Case.ref_no,
        Matter.your_ref == Case.ref_no,
    )


def _active_legacy_case_expr():
    return or_(Case.is_deleted.is_(False), Case.is_deleted.is_(None))


def _legacy_case_only_link_queries():
    """Active legacy rows still keyed by cases.id that cannot resolve to Matter."""
    unresolved_matter_expr = Matter.matter_id.is_(None)

    deadlines_q = (
        db.session.query(
            literal("deadlines").label("table_name"),
            Deadline.id.label("row_id"),
            Deadline.case_id.label("legacy_case_id"),
            Case.ref_no.label("case_ref"),
            Deadline.title.label("title"),
            Deadline.status.label("status"),
        )
        .select_from(Deadline)
        .join(Case, Deadline.case_id == Case.id)
        .outerjoin(Matter, _legacy_case_matter_join_expr())
        .filter(_active_legacy_case_expr())
        .filter(unresolved_matter_expr)
        .filter(func.lower(func.coalesce(Deadline.status, "")) != "done")
    )

    renewal_q = (
        db.session.query(
            literal("renewal_fees").label("table_name"),
            RenewalFee.id.label("row_id"),
            RenewalFee.case_id.label("legacy_case_id"),
            Case.ref_no.label("case_ref"),
            cast(RenewalFee.year, String).label("title"),
            RenewalFee.status.label("status"),
        )
        .select_from(RenewalFee)
        .join(Case, RenewalFee.case_id == Case.id)
        .outerjoin(Matter, _legacy_case_matter_join_expr())
        .filter(_active_legacy_case_expr())
        .filter(unresolved_matter_expr)
        .filter(func.lower(func.coalesce(RenewalFee.status, "")) != "paid")
    )

    queries = [deadlines_q, renewal_q]

    if _legacy_invoice_table_has_case_id():
        invoices_q = (
            db.session.query(
                literal("invoices").label("table_name"),
                Invoice.id.label("row_id"),
                Invoice.case_id.label("legacy_case_id"),
                Case.ref_no.label("case_ref"),
                Invoice.tax_no.label("title"),
                Invoice.status.label("status"),
            )
            .select_from(Invoice)
            .join(Case, Invoice.case_id == Case.id)
            .outerjoin(Matter, _legacy_case_matter_join_expr())
            .filter(_active_legacy_case_expr())
            .filter(unresolved_matter_expr)
            .filter(or_(Invoice.is_deleted.is_(False), Invoice.is_deleted.is_(None)))
            .filter(
                ~func.lower(func.coalesce(Invoice.status, "")).in_(("void", "cancelled", "deleted"))
            )
        )
        queries.append(invoices_q)

    return tuple(queries)


def _legacy_invoice_table_has_case_id() -> bool:
    """Return true only when the deprecated ORM invoice table is physically present."""
    try:
        inspector = inspect(db.engine)
        if not inspector.has_table(Invoice.__tablename__):
            return False
        columns = {
            str(col.get("name") or "") for col in inspector.get_columns(Invoice.__tablename__)
        }
        return {"id", "case_id"}.issubset(columns)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="admin.data_quality_service.legacy_invoice_table_check",
            log_key="admin.data_quality_service.legacy_invoice_table_check",
            log_window_seconds=300,
        )
        return False


def _safe_legacy_case_only_count(query) -> int:
    try:
        return int(query.count() or 0)
    except (OperationalError, ProgrammingError) as exc:
        db.session.rollback()
        report_swallowed_exception(
            exc,
            context="admin.data_quality_service.legacy_case_only_count",
            log_key="admin.data_quality_service.legacy_case_only_count",
            log_window_seconds=300,
        )
        return 0


def _safe_legacy_case_only_rows(query, *, sample_limit: int) -> list[dict]:
    try:
        rows = query.limit(sample_limit).all()
        return [dict(getattr(row, "_mapping", row)) for row in rows]
    except (OperationalError, ProgrammingError) as exc:
        db.session.rollback()
        report_swallowed_exception(
            exc,
            context="admin.data_quality_service.legacy_case_only_rows",
            log_key="admin.data_quality_service.legacy_case_only_rows",
            log_window_seconds=300,
        )
        return []


def get_data_quality_metrics(*, sample_limit: int, parse_days: int) -> dict:
    parse_cutoff = datetime.utcnow() - timedelta(days=parse_days)

    active_matter_expr = or_(Matter.is_deleted.is_(False), Matter.is_deleted.is_(None))
    active_docket_expr = or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None))
    active_annuity_expr = or_(AnnuityItem.is_deleted.is_(False), AnnuityItem.is_deleted.is_(None))
    open_docket_expr = _is_blank_text(DocketItem.done_date)
    name_ref_present_expr = func.trim(func.coalesce(DocketItem.name_ref, "")) != ""
    # Legacy migrated rows (mssql_v2 LimitHistory) are reference records, not actionable
    # operational deadlines. Keep them visible as a separate low-severity bucket.
    legacy_v2_reference_expr = and_(
        func.upper(func.trim(func.coalesce(DocketItem.category, ""))) == "V2_LIMIT",
        _is_blank_text(DocketItem.name_ref),
    )

    orphan_dockets_q = (
        db.session.query(DocketItem)
        .outerjoin(Matter, Matter.matter_id == DocketItem.matter_id)
        .filter(active_docket_expr)
        .filter(or_(Matter.matter_id.is_(None), Matter.is_deleted.is_(True)))
    )
    orphan_annuities_q = (
        db.session.query(AnnuityItem)
        .outerjoin(Matter, Matter.matter_id == AnnuityItem.matter_id)
        .filter(active_annuity_expr)
        .filter(or_(Matter.matter_id.is_(None), Matter.is_deleted.is_(True)))
    )
    orphan_workflows_q = (
        db.session.query(Workflow)
        .outerjoin(Matter, Matter.matter_id == Workflow.case_id)
        .filter(or_(Matter.matter_id.is_(None), Matter.is_deleted.is_(True)))
    )
    dockets_missing_due_q = (
        db.session.query(DocketItem)
        .filter(active_docket_expr)
        .filter(open_docket_expr)
        .filter(_is_blank_text(DocketItem.due_date))
        .filter(~legacy_v2_reference_expr)
    )
    legacy_v2_missing_due_q = (
        db.session.query(DocketItem)
        .filter(active_docket_expr)
        .filter(open_docket_expr)
        .filter(_is_blank_text(DocketItem.due_date))
        .filter(legacy_v2_reference_expr)
    )
    dockets_missing_owner_q = (
        db.session.query(DocketItem)
        .filter(active_docket_expr)
        .filter(open_docket_expr)
        .filter(_is_blank_text(DocketItem.owner_staff_party_id))
    )
    annuity_missing_cycle_q = (
        db.session.query(AnnuityItem)
        .filter(active_annuity_expr)
        .filter(or_(AnnuityItem.cycle_no.is_(None), AnnuityItem.cycle_no <= 0))
    )
    matters_missing_name_q = (
        db.session.query(Matter)
        .filter(active_matter_expr)
        .filter(_is_blank_text(Matter.right_name))
    )
    matters_without_staff_q = (
        db.session.query(Matter)
        .outerjoin(MatterStaffAssignment, MatterStaffAssignment.matter_id == Matter.matter_id)
        .filter(active_matter_expr)
        .filter(MatterStaffAssignment.msa_id.is_(None))
    )
    duplicate_open_dockets_q = (
        db.session.query(
            DocketItem.matter_id.label("matter_id"),
            DocketItem.name_ref.label("name_ref"),
            DocketItem.due_date.label("due_date"),
            func.count(DocketItem.docket_id).label("dup_count"),
        )
        .filter(active_docket_expr)
        .filter(open_docket_expr)
        .filter(name_ref_present_expr)
        .group_by(DocketItem.matter_id, DocketItem.name_ref, DocketItem.due_date)
        .having(func.count(DocketItem.docket_id) > 1)
    )
    legacy_case_only_link_qs = _legacy_case_only_link_queries()

    orphan_dockets_count = orphan_dockets_q.count()
    orphan_annuities_count = orphan_annuities_q.count()
    orphan_workflows_count = orphan_workflows_q.count()
    dockets_missing_due_count = dockets_missing_due_q.count()
    legacy_v2_missing_due_count = legacy_v2_missing_due_q.count()
    dockets_missing_owner_count = dockets_missing_owner_q.count()
    annuity_missing_cycle_count = annuity_missing_cycle_q.count()
    matters_missing_name_count = matters_missing_name_q.count()
    matters_without_staff_count = matters_without_staff_q.count()
    duplicate_open_docket_group_count = duplicate_open_dockets_q.count()
    legacy_case_only_link_count = sum(
        _safe_legacy_case_only_count(q) for q in legacy_case_only_link_qs
    )

    parse_failure_count = 0
    parse_by_kind: list[tuple[str, int]] = []
    parse_recent: list[ParseFailure] = []
    parse_failure_table_missing = False
    try:
        parse_failure_count = (
            db.session.query(func.count(ParseFailure.id))
            .filter(ParseFailure.created_at >= parse_cutoff)
            .scalar()
            or 0
        )
        parse_by_kind = (
            db.session.query(ParseFailure.kind, func.count(ParseFailure.id))
            .filter(ParseFailure.created_at >= parse_cutoff)
            .group_by(ParseFailure.kind)
            .order_by(func.count(ParseFailure.id).desc())
            .all()
        )
        parse_recent = (
            ParseFailure.query.filter(ParseFailure.created_at >= parse_cutoff)
            .order_by(ParseFailure.created_at.desc())
            .limit(sample_limit)
            .all()
        )
    except (ProgrammingError, OperationalError):
        parse_failure_table_missing = True
    except Exception as exc:
        msg = str(exc).lower()
        if "parse_failure" in msg and (
            "no such table" in msg or "does not exist" in msg or "undefinedtable" in msg
        ):
            parse_failure_table_missing = True
        else:
            report_swallowed_exception(
                exc,
                context="admin.data_quality_service.parse_failure",
                log_key="admin.data_quality_service.parse_failure",
                log_window_seconds=300,
            )
            parse_failure_table_missing = True

    dummy_candidates: list[dict] = []
    try:
        dummy_candidates = get_dummy_candidates()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="admin.data_quality_service.get_dummy_candidates",
            log_key="admin.data_quality_service.get_dummy_candidates",
            log_window_seconds=300,
        )
        dummy_candidates = []

    issues = [
        {
            "id": "orphan_dockets",
            "label": "Orphan Dockets",
            "severity": "high",
            "count": orphan_dockets_count,
            "desc": "Docket rows that cannot resolve to an active matter",
        },
        {
            "id": "orphan_annuities",
            "label": "Orphan Annuities",
            "severity": "high",
            "count": orphan_annuities_count,
            "desc": "Annuity rows that cannot resolve to an active matter",
        },
        {
            "id": "orphan_workflows",
            "label": "Orphan Tasks",
            "severity": "high",
            "count": orphan_workflows_count,
            "desc": "Workflow tasks with missing or deleted matter references",
        },
        {
            "id": "docket_missing_due",
            "label": "Dockets Missing Due Date",
            "severity": "high",
            "count": dockets_missing_due_count,
            "desc": "Open docket rows without a due date",
        },
        {
            "id": "docket_missing_owner",
            "label": "Dockets Missing Owner",
            "severity": "medium",
            "count": dockets_missing_owner_count,
            "desc": "Open docket rows without an owner_staff_party_id",
        },
        {
            "id": "annuity_missing_cycle",
            "label": "Annuities Missing Cycle",
            "severity": "medium",
            "count": annuity_missing_cycle_count,
            "desc": "Annuity items with missing or invalid cycle_no",
        },
        {
            "id": "matter_missing_name",
            "label": "Matters Missing Title",
            "severity": "medium",
            "count": matters_missing_name_count,
            "desc": "Active matters without right_name",
        },
        {
            "id": "matter_without_staff",
            "label": "Matters Without Staff",
            "severity": "low",
            "count": matters_without_staff_count,
            "desc": "Active matters without staff assignments",
        },
        {
            "id": "duplicate_open_dockets",
            "label": "Duplicate Open Dockets",
            "severity": "high",
            "count": duplicate_open_docket_group_count,
            "desc": "Duplicate open docket groups by matter_id, name_ref, and due_date",
        },
        {
            "id": "legacy_case_only_links",
            "label": "Unresolved Legacy Case Links",
            "severity": "low",
            "count": legacy_case_only_link_count,
            "desc": "Active legacy rows keyed by cases.id that cannot resolve to Matter",
        },
        {
            "id": "parse_failures_recent",
            "label": f"Recent {parse_days}d Parse Failures",
            "severity": "medium",
            "count": parse_failure_count,
            "desc": "Input parsing failures in the selected date window",
        },
        {
            "id": "dummy_candidates",
            "label": "Placeholder Matters",
            "severity": "low",
            "count": len(dummy_candidates),
            "desc": "Matter records that look like placeholder or test data",
        },
        {
            "id": "legacy_v2_missing_due",
            "label": "Legacy V2_LIMIT Reference Dockets",
            "severity": "low",
            "count": legacy_v2_missing_due_count,
            "desc": "Reference rows imported from LimitHistory without operational due dates",
        },
    ]

    severity_order = {"high": 0, "medium": 1, "low": 2}
    issues = sorted(
        issues,
        key=lambda row: (
            severity_order.get(row.get("severity", "low"), 9),
            -int(row.get("count") or 0),
            str(row.get("label") or ""),
        ),
    )

    weighted_points = sum(
        min(int(item.get("count") or 0), 100)
        * {"high": 5, "medium": 3, "low": 1}.get(item.get("severity", "low"), 1)
        for item in issues
    )
    quality_score = max(0, 100 - min(100, weighted_points // 4))
    score_level = (
        "good" if quality_score >= 90 else ("warning" if quality_score >= 70 else "critical")
    )
    total_issue_count = sum(int(item.get("count") or 0) for item in issues)
    high_issue_count = sum(
        1 for item in issues if item.get("severity") == "high" and int(item.get("count") or 0) > 0
    )
    legacy_case_only_links = []
    for q in legacy_case_only_link_qs:
        legacy_case_only_links.extend(_safe_legacy_case_only_rows(q, sample_limit=sample_limit))
    legacy_case_only_links = legacy_case_only_links[:sample_limit]

    return {
        "quality_score": quality_score,
        "score_level": score_level,
        "total_issue_count": total_issue_count,
        "high_issue_count": high_issue_count,
        "issues": issues,
        "orphan_dockets": orphan_dockets_q.order_by(DocketItem.docket_id.desc())
        .limit(sample_limit)
        .all(),
        "orphan_annuities": orphan_annuities_q.order_by(AnnuityItem.annuity_id.desc())
        .limit(sample_limit)
        .all(),
        "orphan_workflows": orphan_workflows_q.order_by(Workflow.id.desc())
        .limit(sample_limit)
        .all(),
        "dockets_missing_due": dockets_missing_due_q.order_by(DocketItem.docket_id.desc())
        .limit(sample_limit)
        .all(),
        "legacy_v2_missing_due": legacy_v2_missing_due_q.order_by(DocketItem.docket_id.desc())
        .limit(sample_limit)
        .all(),
        "dockets_missing_owner": dockets_missing_owner_q.order_by(DocketItem.docket_id.desc())
        .limit(sample_limit)
        .all(),
        "annuities_missing_cycle": annuity_missing_cycle_q.order_by(AnnuityItem.annuity_id.desc())
        .limit(sample_limit)
        .all(),
        "matters_missing_name": matters_missing_name_q.order_by(Matter.created_at.desc())
        .limit(sample_limit)
        .all(),
        "matters_without_staff": matters_without_staff_q.order_by(Matter.created_at.desc())
        .limit(sample_limit)
        .all(),
        "duplicate_open_docket_groups": duplicate_open_dockets_q.order_by(
            func.count(DocketItem.docket_id).desc()
        )
        .limit(sample_limit)
        .all(),
        "legacy_case_only_links": legacy_case_only_links,
        "parse_failure_count": parse_failure_count,
        "parse_by_kind": parse_by_kind,
        "parse_recent": parse_recent,
        "parse_failure_table_missing": parse_failure_table_missing,
        "dummy_candidates": dummy_candidates[:sample_limit],
    }
