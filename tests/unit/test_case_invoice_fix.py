import pytest

from app.extensions import db
from app.models.legacy_finance import ExternalInvoiceCaseLink, ExternalInvoiceCaseMap
from app.models.ip_records import Matter
from app.services.billing.case_invoice_service import fetch_case_invoice_ids


def test_fetch_case_invoice_ids_integrity(db_session, app):
    # Ensure tables are created (in-memory sqlite)
    with app.app_context():
        db.create_all()

        # Satisfy FK constraints (sqlite may enforce them depending on PRAGMA/config).
        db.session.add(
            Matter(
                matter_id="test_matter_id",
                our_ref="TEST-INVOICE-REF",
                right_name="Test Matter",
            )
        )
        db.session.flush()

        match_map = ExternalInvoiceCaseMap(
            matter_id="test_matter_id", external_invoice_id=123, is_deleted=0  # Using 0 for integer
        )
        match_link = ExternalInvoiceCaseLink(
            matter_id="test_matter_id",
            external_invoice_id=456,
            external_invoice_number="INV-456",
            is_deleted=0,
        )
        db.session.add(match_map)
        db.session.add(match_link)
        db.session.flush()

        # Call the function that failed
        # SQLite won't throw 'integer = boolean' likely, but it verifies model mappings are valid
        ids = fetch_case_invoice_ids("test_matter_id")

        assert 123 in ids
        assert 456 in ids


def test_external_invoice_case_link_uses_ipm_invoice_id_column():
    assert ExternalInvoiceCaseLink.__table__.c.ipm_invoice_id.name == "ipm_invoice_id"
