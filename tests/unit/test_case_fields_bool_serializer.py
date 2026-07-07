from __future__ import annotations

from app.services.case_fields.field_types import FieldDefinition


def test_bool_serializer_blank_is_none():
    fd = FieldDefinition(key="x", label="x", input_type="select_yn", serializer="bool")
    assert fd.serialize(None) is None
    assert fd.serialize("") is None
    assert fd.serialize("   ") is None


def test_bool_serializer_truthy_and_falsy_strings():
    fd = FieldDefinition(key="x", label="x", input_type="select_yn", serializer="bool")
    assert fd.serialize("Y") is True
    assert fd.serialize("yes") is True
    assert fd.serialize("True") is True
    assert fd.serialize("1") is True
    assert fd.serialize("N") is False
    assert fd.serialize("no") is False
    assert fd.serialize("false") is False
    assert fd.serialize("0") is False


def test_bool_serializer_unknown_string_is_left_as_is():
    fd = FieldDefinition(key="x", label="x", input_type="select_yn", serializer="bool")
    assert fd.serialize("maybe") == "maybe"
