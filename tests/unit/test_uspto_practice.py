from __future__ import annotations


def test_uspto_practice_calculates_office_action_response_window():
    from app.services.uspto.uspto_practice import analyze_uspto_document_text

    analysis = analyze_uspto_document_text(
        """
        UNITED STATES PATENT AND TRADEMARK OFFICE
        Non-Final Office Action
        Application No.: 17/123,456
        Mail Date: 01/15/2026
        Applicant: Acme Corporation
        """
    )

    assert analysis.doc_type == "USPTO Non-Final Office Action"
    assert analysis.task_type == "US non-final office action response"
    assert analysis.deadline is not None
    assert analysis.deadline.kind == "office_action_response"
    assert analysis.deadline.due_date == "2026-04-15"
    assert analysis.deadline.statutory_due_date == "2026-07-15"
    assert analysis.deadline.extendable is True


def test_uspto_practice_calculates_notice_of_allowance_issue_fee_deadline():
    from app.services.uspto.uspto_practice import analyze_uspto_document_text

    analysis = analyze_uspto_document_text(
        """
        UNITED STATES PATENT AND TRADEMARK OFFICE
        Notice of Allowance and Fee(s) Due
        Application No.: 17/123,456
        Mail Date: January 15, 2026
        """
    )

    assert analysis.doc_type == "USPTO Notice of Allowance"
    assert analysis.task_type == "US issue fee payment"
    assert analysis.deadline is not None
    assert analysis.deadline.kind == "issue_fee"
    assert analysis.deadline.due_date == "2026-04-15"
    assert analysis.deadline.statutory_due_date == "2026-04-15"
    assert analysis.deadline.extendable is False


def test_uspto_practice_classifies_ids_without_auto_deadline():
    from app.services.uspto.uspto_practice import analyze_uspto_document_text

    analysis = analyze_uspto_document_text(
        """
        PTO/SB/08
        Information Disclosure Statement by Applicant
        Application No.: 17/123,456
        """
    )

    assert analysis.doc_type == "USPTO Information Disclosure Statement"
    assert analysis.task_type == "US IDS review"
    assert analysis.deadline is None
    assert analysis.warnings
