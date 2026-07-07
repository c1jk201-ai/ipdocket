from __future__ import annotations

import time
from datetime import datetime


def _default_value(column) -> str:
    arg = column.default.arg
    try:
        return arg(None)
    except TypeError:
        return arg()


def test_file_asset_link_created_at_defaults_are_runtime_values(app):
    from app.models.communication import CommunicationFileAsset, OfficeActionFileAsset
    from app.models.matter import MatterMemoFileAsset

    for model in (CommunicationFileAsset, OfficeActionFileAsset, MatterMemoFileAsset):
        column = model.__table__.c.created_at
        first = _default_value(column)
        time.sleep(0.01)
        second = _default_value(column)

        assert first != second


def test_matter_memo_file_asset_created_at_default_is_datetime(app):
    from app.models.matter import MatterMemoFileAsset

    value = _default_value(MatterMemoFileAsset.__table__.c.created_at)

    assert isinstance(value, datetime)


def test_legacy_case_relationships_do_not_selectin_load(app):
    from app.models.case import Case

    assert Case.workflows.property.lazy == "dynamic"
    assert Case.deadlines.property.lazy == "select"
    assert Case.renewal_fees.property.lazy == "select"
    assert Case.invoices.property.lazy == "select"
    assert Case.letters.property.lazy == "select"


def test_worklog_status_is_normalized_to_single_vocabulary(app):
    from app.models.worklog import WorkLog

    log = WorkLog(status="In Progress")
    assert log.status == WorkLog.STATUS_IN_PROGRESS

    log.status = "Done"
    assert log.status == WorkLog.STATUS_COMPLETED

    log.status = "unexpected"
    assert log.status == WorkLog.STATUS_PENDING
