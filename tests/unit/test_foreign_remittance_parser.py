from app.services.billing.llm_parser import parse_foreign_remittance_proof_rule_based


def test_foreign_remittance_proof_rule_based_extracts_core_fields():
    text = """
    Text Text
    Text 2026.04.29
    Text EXAMPLE IP LAW OFFICE
    Text ABC LAW FIRM
    Text BANK OF AMERICA
    Text 123456789
    SWIFT BOFAUS3N
    Text USD 1,234.56
    Text 1,700,000
    Text 1377.50
    Text REM-20260429-001
    Text Patent fee
    """

    result = parse_foreign_remittance_proof_rule_based(text)

    assert result["doc_type"] == "foreign_remittance_proof"
    assert result["date"] == "2026-04-29"
    assert result["currency"] == "USD"
    assert result["amount"] == "1234.56"
    assert result["receiver"] == "ABC LAW FIRM"
    assert result["receiver_bank"] == "BANK OF AMERICA"
    assert result["receiver_account"] == "123456789"
    assert result["swift_code"] == "BOFAUS3N"
    assert result["reference"] == "REM-20260429-001"
    assert result["parser"] == "rule"
