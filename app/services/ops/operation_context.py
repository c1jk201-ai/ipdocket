from __future__ import annotations

from typing import Any, Optional

from app.services.ops.backup_service import create_preop_backup
from app.services.ops.operation_log import OperationRecorder


class OperationContext:
    def __init__(
        self,
        action: str,
        *,
        risk_level: str,
        undo_supported: bool,
        undo_deadline_at: Optional[Any] = None,
        targets_json: Optional[dict[str, Any]] = None,
        summary_json: Optional[dict[str, Any]] = None,
        preop_backup_required: Optional[bool] = None,
    ) -> None:
        self._recorder = OperationRecorder(
            action=action,
            risk_level=risk_level,
            undo_supported=undo_supported,
            undo_deadline_at=undo_deadline_at,
            targets_json=targets_json,
            summary_json=summary_json,
        )
        self._preop_backup_required = (
            preop_backup_required if preop_backup_required is not None else risk_level == "HIGH"
        )
        self._started = False

    @property
    def operation(self):
        return self._recorder.operation

    def begin(self) -> "OperationContext":
        if self._started:
            return self
        self._recorder.begin()
        if self._preop_backup_required:
            backup = create_preop_backup(reason=self.operation.action)
            summary = self.operation.summary_json or {}
            summary.setdefault("preop_backup_id", backup.id)
            summary.setdefault("preop_backup_paths", backup.artifact_paths_json)
            self.operation.summary_json = summary
        self._started = True
        return self

    def add_change(
        self,
        *,
        entity_type: str,
        entity_id: str,
        change_type: str,
        before: Optional[dict[str, Any]] = None,
        after: Optional[dict[str, Any]] = None,
        patch: Optional[dict[str, Any]] = None,
        meta: Optional[dict[str, Any]] = None,
    ):
        return self._recorder.add_change(
            entity_type=entity_type,
            entity_id=entity_id,
            change_type=change_type,
            before=before,
            after=after,
            patch=patch,
            meta=meta,
        )

    def commit(self) -> None:
        self._recorder.mark_applied()

    def fail(self, error: Optional[str] = None) -> None:
        self._recorder.mark_failed(error)

    def __enter__(self) -> "OperationContext":
        return self.begin()

    def __exit__(self, exc_type, exc, _tb) -> bool:
        if exc_type:
            self.fail(str(exc))
        return False
