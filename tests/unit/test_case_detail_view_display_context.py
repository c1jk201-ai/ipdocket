from types import SimpleNamespace

from app.blueprints.case.services.detail_context import _build_case_view_display_context


def _build_ctx(**overrides):
    matter = overrides.pop(
        "matter",
        SimpleNamespace(
            matter_type="TRADEMARK",
            right_group="OUT",
            inhouse_status="Text",
            right_name="Matter Title",
        ),
    )
    overview = overrides.pop("overview", SimpleNamespace(right_name="Overview Title"))
    ctx = {
        "matter": matter,
        "overview": overview,
        "case_finance_summary": {},
        "docket_due": [],
        "next_docket": None,
        "dom_patent": {},
        "dom_design": {},
        "dom_trademark": {},
        "inc_patent": {},
        "inc_design": {},
        "inc_trademark": {},
        "out_patent": {},
        "out_design": {},
        "out_trademark": {},
        "pct": {},
        "litigation": {},
        "misc": {},
    }
    ctx.update(overrides)
    return ctx


def test_case_view_display_context_prefers_matching_custom_data_and_madrid_badge() -> None:
    ctx = _build_ctx(
        is_outgoing_trademark=True,
        is_madrid=True,
        out_trademark={"proposal_title": "Madrid Filing", "client_name": "ACME"},
    )

    display = _build_case_view_display_context(ctx)

    assert display["_custom_data"]["proposal_title"] == "Madrid Filing"
    assert display["_badge_label"] == "Madrid · Trademark"
    assert display["_badge_class"] == "badge border border-dark text-dark bg-transparent"
    assert display["case_title"] == "Madrid Filing"
    assert display["case_division"] == "ETC"
    assert display["case_type"] == "MADRID"


def test_case_view_display_context_uses_utility_badge_label() -> None:
    ctx = _build_ctx(
        matter=SimpleNamespace(
            matter_type="UTILITY",
            right_group="DOM",
            inhouse_status="Text",
            right_name="Utility Matter",
        ),
        is_domestic_patent=True,
    )

    display = _build_case_view_display_context(ctx)

    assert display["_badge_label"] == "US · Utility"
    assert display["_badge_class"] == "badge bg-secondary"
    assert display["case_title"] == "Utility Matter"


def test_case_view_display_context_treats_pct_matter_type_as_pct_even_without_flag() -> None:
    ctx = _build_ctx(
        matter=SimpleNamespace(
            matter_type="PCT",
            right_group="OUT",
            inhouse_status="Text",
            right_name="Pct Matter",
        ),
        pct={"title": "PCT Title"},
    )

    display = _build_case_view_display_context(ctx)

    assert display["_custom_data"]["title"] == "PCT Title"
    assert display["_badge_label"] == "PCT"
    assert display["_badge_class"] == "badge border border-dark text-dark bg-transparent"
    assert display["case_title"] == "PCT Title"
    assert display["case_division"] == "ETC"
    assert display["case_type"] == "PCT"


def test_case_view_display_context_exposes_copyright_as_etc_kind() -> None:
    ctx = _build_ctx(
        matter=SimpleNamespace(
            matter_type="MISC",
            right_group="",
            inhouse_status="Text",
            right_name="Copyright Matter",
        ),
        is_misc=True,
        is_copyright=True,
        misc={"case_kind": "Text", "title": "Copyright Title"},
    )

    display = _build_case_view_display_context(ctx)

    assert display["_badge_label"] == "Copyright"
    assert display["_badge_class"] == "badge bg-warning text-dark"
    assert display["case_division"] == "ETC"
    assert display["case_type"] == "COPYRIGHT"
    assert display["case_title"] == "Copyright Title"


def test_case_view_display_context_keeps_outline_fallback_for_incoming_cases() -> None:
    ctx = _build_ctx(
        matter=SimpleNamespace(
            matter_type="SPECIAL",
            right_group="INC",
            inhouse_status="",
            right_name="",
        ),
        overview=SimpleNamespace(right_name="Overview Fallback"),
    )

    display = _build_case_view_display_context(ctx)

    assert display["_badge_label"] == "INC SPECIAL"
    assert display["_badge_class"] == "badge border border-secondary text-secondary bg-transparent"
    assert display["case_title"] == "Overview Fallback"
