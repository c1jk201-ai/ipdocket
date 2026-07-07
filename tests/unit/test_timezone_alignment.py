from __future__ import annotations

import os
from datetime import date, datetime


def test_app_timezone_default_is_eastern(app):
    assert (app.config.get("TIMEZONE") or "").strip() == "America/New_York"


def test_app_locale_and_date_formats_default_to_us(app):
    assert (app.config.get("LOCALE") or "").strip() == "en-US"
    assert app.config.get("DATE_FORMAT") == "%m/%d/%Y"
    assert app.config.get("DATETIME_FORMAT") == "%m/%d/%Y %I:%M:%S %p"
    assert app.config.get("DATETIME_MINUTE_FORMAT") == "%m/%d/%Y %I:%M %p"


def test_template_filters_render_us_date_and_eastern_time(app):
    local_dt = app.jinja_env.filters["local_dt"]
    local_dt_min = app.jinja_env.filters["local_dt_min"]
    us_date = app.jinja_env.filters["us_date"]
    date_only = app.jinja_env.filters["date_only"]

    assert us_date("2026-03-04") == "03/04/2026"
    assert date_only("2026-03-04 12:00:00") == "2026-03-04"
    assert local_dt(datetime(2026, 7, 3, 16, 5, 6)) == "07/03/2026 12:05:06 PM"
    assert local_dt_min(datetime(2026, 7, 3, 16, 5, 6)) == "07/03/2026 12:05 PM"


def test_apply_process_timezone_sets_tz_env(monkeypatch):
    from app.utils.timezone import apply_process_timezone

    monkeypatch.delenv("TZ", raising=False)
    applied = apply_process_timezone("America/New_York")
    assert applied == "America/New_York"
    assert (os.environ.get("TZ") or "").strip() == "America/New_York"


def test_workflow_urgent_overdue_uses_local_today(monkeypatch):
    import app.models.workflow as workflow_module
    from app.models.workflow import Workflow

    monkeypatch.setattr(workflow_module, "today_local", lambda: date(2026, 2, 23))

    due_today = Workflow(status="Pending", due_date=date(2026, 2, 23))
    assert due_today.is_urgent is True
    assert due_today.is_overdue is False

    overdue = Workflow(status="Pending", due_date=date(2026, 2, 22))
    assert overdue.is_overdue is True
    assert overdue.is_urgent is False

    abandoned_due_today = Workflow(status="Abandoned", due_date=date(2026, 2, 23))
    assert abandoned_due_today.is_urgent is False
    assert abandoned_due_today.is_overdue is False

    abandoned_overdue = Workflow(status="Abandoned", due_date=date(2026, 2, 22))
    assert abandoned_overdue.is_urgent is False
    assert abandoned_overdue.is_overdue is False


def test_productivity_get_today_uses_timezone_helper(monkeypatch):
    import app.services.productivity.utils as productivity_utils

    expected = date(2031, 1, 2)
    monkeypatch.setattr(productivity_utils, "today_local", lambda: expected)
    assert productivity_utils.get_today() == expected
