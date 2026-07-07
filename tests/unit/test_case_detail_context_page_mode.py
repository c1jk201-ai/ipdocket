from app.blueprints.case.services import detail_context


def test_build_case_detail_context_page_mode_uses_light_history(monkeypatch):
    monkeypatch.setattr(
        detail_context,
        "_build_base",
        lambda mid, request_args, current_user: {
            "matter": object(),
            "overview": None,
            "_mid_str": str(mid),
        },
    )
    monkeypatch.setattr(detail_context, "_build_auto_status_section", lambda ctx: {})

    history_calls = []

    def _fake_history_section(ctx, *, include_history_details=True, **_kwargs):
        history_calls.append(include_history_details)
        return {"history_total_count": 7, "history_rows": [], "_history_count": 7}

    monkeypatch.setattr(detail_context, "_build_history_section", _fake_history_section)
    monkeypatch.setattr(detail_context, "_build_family_section", lambda ctx: {})
    monkeypatch.setattr(detail_context, "_build_specific_fields_section", lambda ctx: {})
    monkeypatch.setattr(detail_context, "_build_costs_section", lambda ctx, **_kwargs: {})
    monkeypatch.setattr(detail_context, "_build_case_view_display_context", lambda ctx: {})
    monkeypatch.setattr(
        detail_context,
        "_build_file_manager_section",
        lambda ctx, request_args, counts_only=False: {},
    )
    monkeypatch.setattr(detail_context, "_build_audit_section", lambda ctx: {})
    monkeypatch.setattr(detail_context, "_build_alarm_section", lambda ctx, **_kwargs: {})

    ctx = detail_context.build_case_detail_context("mid-1", {}, object())

    assert history_calls == [False]
    assert ctx["history_total_count"] == 7


def test_build_case_detail_context_history_panel_returns_early(monkeypatch):
    monkeypatch.setattr(
        detail_context,
        "_build_base",
        lambda mid, request_args, current_user: {
            "matter": object(),
            "overview": None,
            "_mid_str": str(mid),
        },
    )
    monkeypatch.setattr(
        detail_context,
        "_build_history_panel_context",
        lambda ctx: {"history_total_count": 3, "history_rows": [{"id": "row-1"}]},
    )
    monkeypatch.setattr(
        detail_context,
        "_build_auto_status_section",
        lambda ctx: (_ for _ in ()).throw(AssertionError("unexpected full-page work")),
    )

    ctx = detail_context.build_case_detail_context("mid-2", {}, object(), view_mode="history_panel")

    assert ctx["history_total_count"] == 3
    assert ctx["history_rows"] == [{"id": "row-1"}]


def test_build_case_detail_context_deadlines_panel_uses_light_section(monkeypatch):
    monkeypatch.setattr(
        detail_context,
        "_build_base",
        lambda mid, request_args, current_user: {
            "matter": object(),
            "overview": None,
            "_mid_str": str(mid),
        },
    )
    monkeypatch.setattr(
        detail_context,
        "_build_deadlines_panel_context",
        lambda ctx: {"docket_open": ["docket-1"], "today_iso": "2026-04-24"},
    )
    monkeypatch.setattr(
        detail_context,
        "_build_history_section",
        lambda ctx, **kwargs: (_ for _ in ()).throw(
            AssertionError("deadlines panel should not build full history section")
        ),
    )

    ctx = detail_context.build_case_detail_context(
        "mid-3", {}, object(), view_mode="deadlines_panel"
    )

    assert ctx["docket_open"] == ["docket-1"]
    assert ctx["today_iso"] == "2026-04-24"


def test_build_case_detail_context_memo_panel_uses_light_section(monkeypatch):
    monkeypatch.setattr(
        detail_context,
        "_build_base",
        lambda mid, request_args, current_user: {
            "matter": object(),
            "overview": None,
            "_mid_str": str(mid),
        },
    )
    monkeypatch.setattr(
        detail_context,
        "_build_memo_panel_context",
        lambda ctx: {"memos": ["memo-1"]},
    )
    monkeypatch.setattr(
        detail_context,
        "_build_history_section",
        lambda ctx, **kwargs: (_ for _ in ()).throw(
            AssertionError("memo panel should not build full history section")
        ),
    )

    ctx = detail_context.build_case_detail_context("mid-4", {}, object(), view_mode="memo_panel")

    assert ctx["memos"] == ["memo-1"]
