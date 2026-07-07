from __future__ import annotations


def test_uspto_filing_receipt_rule_parser_extracts_core_fields():
    from app.services.uspto.uspto_form_parser import parse_uspto_form_rule_based

    text = """
    UNITED STATES PATENT AND TRADEMARK OFFICE
    Filing Receipt
    Application No.: 17/123,456
    Filing Date: 01/31/2024
    Attorney Docket No.: 26PD0117US
    Confirmation No.: 9123
    Title of Invention: DEVICE FOR MANAGING CASE DATA
    Applicant: Acme Corporation
    First Named Inventor: Jane Doe
    """

    parsed = parse_uspto_form_rule_based(text, filename="filing_receipt.pdf")

    assert parsed.doc_type == "USPTO Filing Receipt"
    assert parsed.matter_kind == "patent"
    assert parsed.app_no == "17/123,456"
    assert parsed.attorney_docket_no == "26PD0117US"
    assert parsed.confirmation_no == "9123"
    assert parsed.filing_date == "2024-01-31"
    assert parsed.title == "DEVICE FOR MANAGING CASE DATA"
    assert parsed.applicant_name == "Acme Corporation"


def test_uspto_teas_parser_keeps_trademark_serial_as_digits():
    from app.services.uspto.uspto_form_parser import parse_uspto_form_rule_based

    text = """
    USPTO TEAS Trademark Electronic Application System
    Serial Number: 98765432
    Mark: SAMPLEMARK
    Owner: Example IP LLC
    """

    parsed = parse_uspto_form_rule_based(text, filename="teas_receipt.pdf")

    assert parsed.doc_type == "USPTO TEAS Form"
    assert parsed.matter_kind == "trademark"
    assert parsed.app_no == "98765432"
    assert parsed.mark_name == "SAMPLEMARK"
    assert parsed.title == "SAMPLEMARK"
