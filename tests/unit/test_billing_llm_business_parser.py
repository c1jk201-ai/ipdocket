from app.services.billing.llm_parser import parse_business_registration_rule_based


def test_business_document_rule_based_extracts_us_ein() -> None:
    text = "\n".join(
        [
            "Employer Identification Number: 12-3456789",
            "Business name: Example IP LLC",
            "Representative: Jane Partner",
            "Business address: 123 Main St, Alexandria, VA",
            "Principal office: 456 Market St, Arlington, VA",
            "Entity type: Limited liability company",
            "billing@example.com",
        ]
    )

    result = parse_business_registration_rule_based(text)

    assert result["reg_number"] == "12-3456789"
    assert result["company_name"] == "Example IP LLC"
    assert result["representative_name"] == "Jane Partner"
    assert result["business_location"] == "123 Main St, Alexandria, VA"
    assert result["head_office_location"] == "456 Market St, Arlington, VA"
    assert result["business_type"] == "Limited liability company"
    assert result["tax_invoice_email"] == "billing@example.com"


def test_business_document_rule_based_formats_unhyphenated_ein() -> None:
    result = parse_business_registration_rule_based(
        "EIN: 123456789\nBusiness name: Example LLC"
    )

    assert result["reg_number"] == "12-3456789"
