"""Anti-corruption helpers for legacy Case access.

New code should use Matter.matter_id directly. This adapter is the boundary for
the remaining Case.ref_no compatibility paths.
"""

from __future__ import annotations

from app.services.matter.matter_identity_service import MatterIdentityService
from app.utils.error_logging import report_swallowed_exception


class LegacyCaseAdapter:
    @staticmethod
    def get_case(case_id):
        from app.extensions import db
        from app.models.case import Case

        return db.session.get(Case, case_id)

    @staticmethod
    def get_case_or_404(case_id: int):
        from app.models.case import Case

        return Case.query.get_or_404(case_id)

    @staticmethod
    def matter_id_for_case(case) -> str | None:
        return MatterIdentityService.resolve_matter_id_for_case_ref(getattr(case, "ref_no", None))

    @staticmethod
    def can_access(user, case, action: str = "view") -> bool:
        if not case:
            return False
        matter_id = LegacyCaseAdapter.matter_id_for_case(case)
        if matter_id:
            try:
                from app.utils.permissions import can_access_matter

                return can_access_matter(user, matter_id, action)
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="legacy_case_adapter.can_access.matter_policy",
                    log_key="legacy_case_adapter.can_access.matter_policy",
                    log_window_seconds=300,
                )
                return False
        return LegacyCaseAdapter._can_access_unmapped_case(user, case, action)

    @staticmethod
    def _can_access_unmapped_case(user, case, action: str = "view") -> bool:
        if not user or not getattr(user, "is_authenticated", False):
            return False
        if getattr(user, "is_active", True) is False:
            return False

        try:
            from app.utils.permissions import can_manage_case_globally

            if action == "delete_case":
                return can_manage_case_globally(user)
            if can_manage_case_globally(user):
                return True
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="legacy_case_adapter.can_access.global_policy",
                log_key="legacy_case_adapter.can_access.global_policy",
                log_window_seconds=300,
            )

        uid = getattr(user, "id", None)
        if not uid:
            return False
        return uid == getattr(case, "manager_id", None) or uid == getattr(case, "attorney_id", None)
