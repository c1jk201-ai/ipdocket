from __future__ import annotations

from app.services.case.case_parameter_service import CaseParameterService


def test_get_case_profile_preserves_etc_madrid_profile_kind() -> None:
    profile = CaseParameterService.get_case_profile("ETC", "MADRID")

    assert profile.division == "ETC"
    assert profile.case_type == "MADRID"
    assert profile.mapping_division == "OUT"
    assert profile.mapping_type == "TRADEMARK"
    assert profile.group == "TRADEMARK"
    assert profile.namespace == "outgoing_trademark"
    assert profile.arg_key == "out_tm"


def test_get_case_profile_preserves_etc_copyright_profile_kind() -> None:
    profile = CaseParameterService.get_case_profile("ETC", "COPYRIGHT")

    assert profile.division == "ETC"
    assert profile.case_type == "COPYRIGHT"
    assert profile.mapping_division == ""
    assert profile.mapping_type == "MISC"
    assert profile.group == "MISC"
    assert profile.namespace == "misc"
    assert profile.arg_key == "misc"
    assert profile.auto_status is False
