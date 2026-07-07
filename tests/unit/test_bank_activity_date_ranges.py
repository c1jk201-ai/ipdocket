from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.blueprints.billing_invoices.routes import bank_activity


def test_add_months_clamped_handles_month_end():
    assert bank_activity._add_months_clamped(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert bank_activity._add_months_clamped(date(2026, 3, 31), -1) == date(2026, 2, 28)


def test_format_eastern_timestamp_accepts_rfc1123_gmt():
    assert (
        bank_activity._format_kst_timestamp("Mon, 09 Mar 2026 23:00:09 GMT") == "2026-03-09 19:00:09"
    )


def test_bank_activity_page_defaults_to_calendar_month(monkeypatch, admin_client):
    monkeypatch.setattr(
        bank_activity,
        "_now_in_tz",
        lambda: datetime(2026, 3, 10, 9, 0, 0, tzinfo=ZoneInfo("America/New_York")),
    )

    res = admin_client.get("/accounting/invoice-system/bank_activity/page")

    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert 'value="2026-02-10"' in html
    assert 'value="2026-03-10"' in html
    assert 'id="chkDeposit" checked' in html
    assert 'id="chkWithdraw" checked' not in html
    assert 'id="chkEtc" checked' not in html
    assert "<h2 class=\"mb-0\">Bank activity</h2>" in html
    assert "jobStateExpenses'-'" not in html
    assert "errorCodeExpenses'-'" not in html
    assert "lj.jobState || '-'" in html
    assert "lj.errorCode || '-'" in html


def test_bank_activity_matching_page_renders_recommend_filter_ui(admin_client):
    res = admin_client.get("/accounting/invoice-system/bank_activity/matching")

    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert 'id="btnApplyFilters"' in html
    assert 'id="btn3m"' in html
    assert 'id="btn6m"' in html
    assert 'id="btn12m"' in html
    assert "date-preset-group" in html
    assert 'id="activeAccountsSummary"' in html
    assert 'id="bpSelectionSummary"' in html
    assert 'id="invoiceResultSummary"' in html
    assert 'id="depositResultSummary"' in html
    assert 'id="invoiceRecommendFilterBox"' in html
    assert 'id="depositRecommendFilterBox"' in html
    assert "Deposit Matching" in html
    assert "Last 3 months" in html


def test_bank_activity_matching_page_defaults_to_three_months(monkeypatch, admin_client):
    monkeypatch.setattr(
        bank_activity,
        "_now_in_tz",
        lambda: datetime(2026, 3, 10, 9, 0, 0, tzinfo=ZoneInfo("America/New_York")),
    )

    res = admin_client.get("/accounting/invoice-system/bank_activity/matching")

    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert 'value="2025-12-10"' in html
    assert 'value="2026-03-10"' in html


def test_bank_activity_active_segmented_controls_keep_text_contrast():
    repo_root = Path(__file__).resolve().parents[2]
    css = (repo_root / "app/static/billing_invoices/styles.css").read_text(encoding="utf-8")

    required_selectors = [
        ".bank_activity-currency-switch__option.is-active:visited",
        ".bank_activity-currency-switch__option.is-active:hover",
        ".bank_activity-currency-switch__option.is-active:focus",
        ".mode-toggle :is(a, button).active:visited",
        ".mode-toggle :is(a, button).active:hover",
        ".mode-toggle :is(a, button).active:focus",
        ".date-preset-group .date-preset-btn.is-active:hover",
        ".date-preset-group .date-preset-btn.is-active:focus",
    ]

    for selector in required_selectors:
        assert selector in css

    active_block_start = css.index(".invoice-theme .bank_activity-currency-switch__option.is-active")
    active_block_end = css.index("}", active_block_start)
    active_block = css[active_block_start:active_block_end]
    assert "color: #fff;" in active_block
    assert "text-decoration: none;" in active_block
