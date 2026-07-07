from app.services.case.case_parameter_service import CaseParameterService


def _has_missing_key(missing: list[dict], key: str) -> bool:
    return any((item or {}).get("key") == key for item in (missing or []))


def test_client_name_is_required_dom_patent():
    missing = CaseParameterService.validate_required_fields({}, "DOM", "PATENT")
    assert _has_missing_key(missing, "client_name")

    missing_after = CaseParameterService.validate_required_fields(
        {"client_name": "Text"}, "DOM", "PATENT"
    )
    assert not _has_missing_key(missing_after, "client_name")


def test_client_name_is_required_misc():
    missing = CaseParameterService.validate_required_fields({}, "", "MISC")
    assert _has_missing_key(missing, "client_name")
