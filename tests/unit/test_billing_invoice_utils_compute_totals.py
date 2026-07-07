from decimal import Decimal

from app.services.billing.utils import compute_totals


def test_compute_totals_includes_negative_admin_adjustments():
    items = [
        {
            "qty": 1,
            "unit_price": 722000,
            "discount": 70,
            "item_type": "admin",
            "is_taxable": 0,
            "is_estimated": 0,
        },
        {
            "qty": 3,
            "unit_price": 46000,
            "discount": 0,
            "item_type": "admin",
            "is_taxable": 0,
            "is_estimated": 0,
        },
        {
            "qty": 1,
            "unit_price": -140100,
            "discount": 0,
            "item_type": "admin",
            "is_taxable": 0,
            "is_estimated": 0,
        },
        {
            "qty": 3,
            "unit_price": -52000,
            "discount": 0,
            "item_type": "admin",
            "is_taxable": 0,
            "is_estimated": 0,
        },
    ]

    subtotal, tax, total = compute_totals(items, Decimal("10"))

    assert subtotal == Decimal("58500.00")
    assert tax == Decimal("0.00")
    assert total == Decimal("58500.00")


def test_compute_totals_applies_vat_after_negative_taxable_adjustment():
    items = [
        {
            "qty": 1,
            "unit_price": 100000,
            "discount": 0,
            "item_type": "service",
            "is_taxable": 1,
            "is_estimated": 0,
        },
        {
            "qty": 1,
            "unit_price": -10000,
            "discount": 0,
            "item_type": "service",
            "is_taxable": 1,
            "is_estimated": 0,
        },
    ]

    subtotal, tax, total = compute_totals(items, Decimal("10"))

    assert subtotal == Decimal("90000.00")
    assert tax == Decimal("9000.00")
    assert total == Decimal("99000.00")
