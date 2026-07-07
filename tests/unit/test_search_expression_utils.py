from app.utils.search import (
    extract_positive_search_terms,
    matches_search_expression,
    parse_search_expression,
)


def test_parse_search_expression_supports_or_not_and_fields():
    expr = parse_search_expression(
        'client:"Alpha Co" OR ref:US-123 -blocked',
        field_aliases={"client": "client", "ref": "our_ref"},
    )

    assert len(expr.groups) == 2
    assert expr.groups[0].fields == {"client": ["Alpha Co"]}
    assert expr.groups[1].fields == {"our_ref": ["US-123"]}
    assert expr.groups[1].not_terms == ["blocked"]


def test_matches_search_expression_supports_fields_and_negation():
    expr = parse_search_expression(
        "client:alpha -blocked",
        field_aliases={"client": "client"},
    )

    assert matches_search_expression(
        "allowed alpha project",
        expr,
        field_values={"client": "Alpha IP"},
    )
    assert not matches_search_expression(
        "blocked alpha project",
        expr,
        field_values={"client": "Alpha IP"},
    )
    assert not matches_search_expression(
        "allowed alpha project",
        expr,
        field_values={"client": "Beta IP"},
    )


def test_extract_positive_search_terms_collects_terms_and_field_values():
    expr = parse_search_expression(
        '"exact phrase" OR client:alpha -beta',
        field_aliases={"client": "client"},
    )

    assert extract_positive_search_terms(expr) == ["exact phrase", "alpha"]
