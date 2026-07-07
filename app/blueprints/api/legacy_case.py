"""Legacy Case-id API compatibility endpoints."""

from __future__ import annotations

from flask import jsonify
from flask_login import current_user, login_required
from sqlalchemy.exc import SQLAlchemyError

from app.blueprints.api import bp
from app.models.workflow import Workflow
from app.services.case.legacy_case_adapter import LegacyCaseAdapter
from app.utils.error_logging import report_swallowed_exception
from app.utils.legacy_compat import legacy_compat_endpoint

LEGACY_CASE_API_REPLACEMENT = "/api/cases/<matter_id>/summary"


def _can_view_case_row(case) -> bool:
    return LegacyCaseAdapter.can_access(current_user, case, action="view")


@bp.route("/case/<int:case_id>/relations", methods=["GET"])
@legacy_compat_endpoint(
    compat_id="api-case-id",
    successor=LEGACY_CASE_API_REPLACEMENT,
)
@login_required
def case_relations(case_id: int):
    case = LegacyCaseAdapter.get_case_or_404(case_id)
    if not _can_view_case_row(case):
        return jsonify({"error": "forbidden"}), 403
    client = case.client
    flows = []
    try:
        flows = case.workflows.order_by(Workflow.due_date.desc()).limit(20).all()
    except SQLAlchemyError as exc:
        report_swallowed_exception(
            exc,
            context="api.legacy_case.case_relations.workflows",
            log_key="api.legacy_case.case_relations.workflows",
            log_window_seconds=300,
        )
        fallback_case_id = LegacyCaseAdapter.matter_id_for_case(case) or str(case_id)
        try:
            flows = (
                Workflow.query.filter_by(case_id=str(fallback_case_id))
                .order_by(Workflow.due_date.desc())
                .limit(20)
                .all()
            )
        except SQLAlchemyError as exc2:
            report_swallowed_exception(
                exc2,
                context="api.legacy_case.case_relations.workflows_fallback",
                log_key="api.legacy_case.case_relations.workflows_fallback",
                log_window_seconds=300,
            )
            flows = []

    return jsonify(
        {
            "client": {
                "id": client.id if client else None,
                "name": client.name if client else None,
            },
            "workflows": [
                {
                    "id": f.id,
                    "name": f.name,
                    "due_date": f.due_date.isoformat() if f.due_date else None,
                    "status": f.status,
                }
                for f in flows
            ],
        }
    )
