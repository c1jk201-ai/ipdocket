from __future__ import annotations

from app.extensions import db
from app.services.matter.office_action_auto_close_service import auto_close_handled_office_actions
from app.utils.error_logging import report_swallowed_exception


def run_office_action_auto_close(
    *,
    matter_id: str | None = None,
    limit_matters: int = 200,
) -> dict:
    result = auto_close_handled_office_actions(
        matter_id=matter_id,
        limit_matters=limit_matters,
        commit=False,
    )
    try:
        db.session.commit()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="office_action_auto_close_usecase.commit",
            log_key="office_action_auto_close_usecase.commit",
            log_window_seconds=300,
        )
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="office_action_auto_close_usecase.rollback",
                log_key="office_action_auto_close_usecase.rollback",
                log_window_seconds=300,
            )
    return result
