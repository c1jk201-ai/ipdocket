from decimal import Decimal

from werkzeug.datastructures import MultiDict

from app.blueprints.billing_invoices.services.invoice_creation_service import (
    _collect_submitted_items,
    _normalize_items_for_save,
    parse_invoice_prefill_items,
)
from app.services.billing.utils import compute_totals


def test_admin_fee_submission_ignores_fx_fields():
    form = MultiDict(
        [
            ("description[]", "Text Text"),
            ("qty[]", "1"),
            ("unit_price[]", "46000"),
            ("item_type[]", "admin"),
            ("discount[]", "0"),
            ("phase[]", "app"),
            ("fx_currency[]", "KWD"),
            ("fx_fee[]", "100"),
            ("fx_gov[]", "200"),
            ("fx_markup[]", "2"),
            ("fx_rate_used[]", "4300"),
            ("is_estimated_base[]", "0"),
            ("foreign_vat_base[]", "1"),
        ]
    )

    submitted, foreign_vat_bases = _collect_submitted_items(form)
    normalized = _normalize_items_for_save(submitted, foreign_vat_bases)

    assert submitted[0]["item_type"] == "admin"
    assert submitted[0]["fx_currency"] is None
    assert normalized[0]["fx_currency"] is None
    assert normalized[0]["fx_fee"] is None
    assert normalized[0]["fx_gov"] is None
    assert normalized[0]["fx_markup"] is None
    assert normalized[0]["fx_rate_used"] is None
    assert normalized[0]["is_taxable"] == 0

    subtotal, tax, total = compute_totals(normalized, Decimal("10"))

    assert subtotal == Decimal("46000.00")
    assert tax == Decimal("0.00")
    assert total == Decimal("46000.00")


def test_invoice_prefill_items_support_json_and_localized_aliases():
    form = MultiDict(
        [
            (
                "items",
                (
                    '[{"desc":"Text Text","type":"fee","quantity":"1.0",'
                    '"price":"2000000.0","discount_pct":"10.0","estimated":"yes"},'
                    '{"description":"Text Text","category":"official fee","qty":"1",'
                    '"unit_price":"46,000","discount":"70","estimated":"actual"}]'
                ),
            )
        ]
    )

    items = parse_invoice_prefill_items(form)

    assert items == [
        {
            "description": "Text Text",
            "qty": "1.0",
            "unit_price": "2000000.0",
            "item_type": "service",
            "discount": "10.0",
            "phase": "app",
            "is_estimated": 1,
            "is_taxable": 1,
        },
        {
            "description": "Text Text",
            "qty": "1",
            "unit_price": "46000",
            "item_type": "admin",
            "discount": "70",
            "phase": "app",
            "is_estimated": 0,
            "is_taxable": 0,
        },
    ]


def test_invoice_prefill_items_support_repeated_delimited_rows():
    form = MultiDict(
        [
            (
                "item",
                "Text Text|yes|fee|1.0|2000000.0|10.0|1,800,000",
            ),
            (
                "item",
                "Text Text Text|yes|official fee|1.0|166000.0|70.0|49,800",
            ),
        ]
    )

    items = parse_invoice_prefill_items(form)

    assert [item["description"] for item in items] == [
        "Text Text",
        "Text Text Text",
    ]
    assert [item["item_type"] for item in items] == ["service", "admin"]
    assert [item["is_estimated"] for item in items] == [1, 1]
    assert items[1]["unit_price"] == "166000.0"
    assert items[1]["discount"] == "70.0"


def test_invoice_prefill_items_support_parallel_form_style_params():
    form = MultiDict(
        [
            ("description[]", "Text Text Text"),
            ("item_type[]", "fee"),
            ("qty[]", "1"),
            ("unit_price[]", "200000"),
            ("discount[]", "0"),
            ("is_estimated_base[]", "true"),
            ("description[]", "Text Text Text"),
            ("item_type[]", "official fee"),
            ("qty[]", "1"),
            ("unit_price[]", "200000"),
            ("discount[]", "0"),
            ("is_estimated_base[]", "true"),
        ]
    )

    items = parse_invoice_prefill_items(form)

    assert len(items) == 2
    assert items[0]["item_type"] == "service"
    assert items[1]["item_type"] == "admin"
    assert all(item["is_estimated"] == 1 for item in items)
