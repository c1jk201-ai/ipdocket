from app.services.assistant.llm_document_parser import (
    _normalize_foreign_email_extract_payload,
    parse_foreign_email_extract_with_usage,
)
from app.services.mail.foreign_email_schema import FOREIGN_EMAIL_EXTRACT_V1_SCHEMA


def test_normalize_foreign_email_extract_payload_converts_list_to_dict():
    payload = {
        "schema": "foreign_email_extract_v1",
        "evidence_map": [
            {
                "field_path": "params.response_deadline",
                "attachment_sha256": None,
                "page": 1,
                "snippet": "Response due by 2025-03-01",
                "char_start": 24,
                "char_end": 34,
            }
        ],
    }

    out = _normalize_foreign_email_extract_payload(payload)

    assert out["evidence_map"] == {
        "params.response_deadline": {
            "attachment_sha256": None,
            "page": 1,
            "snippet": "Response due by 2025-03-01",
            "char_start": 24,
            "char_end": 34,
        }
    }


def test_normalize_foreign_email_extract_payload_preserves_duplicate_evidence():
    payload = {
        "evidence_map": [
            {
                "field_path": "params.response_deadline",
                "attachment_sha256": "a",
                "page": 1,
                "snippet": "first",
                "char_start": 10,
                "char_end": 20,
            },
            {
                "field_path": "params.response_deadline",
                "attachment_sha256": "b",
                "page": 2,
                "snippet": "second",
                "char_start": 30,
                "char_end": 40,
            },
        ]
    }

    out = _normalize_foreign_email_extract_payload(payload)

    evidence = out["evidence_map"]["params.response_deadline"]
    assert evidence["snippet"] == "first"
    assert evidence["additional_evidence"] == [
        {
            "attachment_sha256": "b",
            "page": 2,
            "snippet": "second",
            "char_start": 30,
            "char_end": 40,
        }
    ]


def test_normalize_foreign_email_extract_payload_keeps_existing_dict():
    payload = {"evidence_map": {"params.oa_date": {"snippet": "OA date"}}}

    out = _normalize_foreign_email_extract_payload(payload)

    assert out["evidence_map"] == {"params.oa_date": {"snippet": "OA date"}}


def test_normalize_foreign_email_extract_payload_coerces_invalid_type():
    payload = {"evidence_map": "not-a-map"}

    out = _normalize_foreign_email_extract_payload(payload)

    assert out["evidence_map"] == {}


def test_foreign_email_extract_schema_uses_evidence_map_array():
    evidence_map = FOREIGN_EMAIL_EXTRACT_V1_SCHEMA["schema"]["properties"]["evidence_map"]
    assert evidence_map["type"] == "array"

    item_schema = evidence_map["items"]
    assert item_schema["type"] == "object"
    assert item_schema["properties"]["field_path"]["type"] == "string"
    assert "char_start" in item_schema["required"]
    assert "char_end" in item_schema["required"]


def test_foreign_email_extract_rule_fallback_returns_structured_payload_without_api_key():
    text = """
    Our Ref: 25PD0123US
    Application No.: 10-2024-0123456
    Title: Widget control system
    Response due by 2026-03-01
    """

    payload, method, usage = parse_foreign_email_extract_with_usage(text, api_key=None)

    assert method == "rule"
    assert usage == {}
    assert payload["schema"] == "foreign_email_extract_v1"
    assert payload["case_target"]["match_keys"]["our_ref"] == "25PD0123US"
    assert payload["case_target"]["match_keys"]["application_no"] == "10-2024-0123456"
    assert payload["params"]["application_no"] == "10-2024-0123456"
    assert payload["params"]["right_name"] == "Widget control system"
    assert payload["params"]["response_deadline"] == "2026-03-01"
    assert payload["dockets"][0]["due_date"] == "2026-03-01"
    assert "params.response_deadline" in payload["evidence_map"]
    assert "dockets[0].due_date" in payload["evidence_map"]
