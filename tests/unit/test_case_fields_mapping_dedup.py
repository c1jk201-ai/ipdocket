from __future__ import annotations

from app.services.case_fields.mapping_service import MappingService


class _RegistryStub:
    def __init__(self, *, existing: set[str]):
        self._existing = set(existing)

    def exists(self, key: str) -> bool:
        return key in self._existing


def test_mapping_service_dedupes_duplicate_field_keys_and_merges_required():
    service = MappingService.instance()
    target = {}
    mapping = {
        "namespace": "test",
        "fields": [
            {"key": "a", "order": 2, "col": 1, "required": False},
            {"key": "a", "order": 1, "col": 2, "required": True},
            {"key": "b", "order": 3, "col": 1, "required": False},
            {"key": "b", "order": 3, "col": 1, "required": True},
        ],
        "extra_allowed": [],
    }

    service._load_mapping(target, "TEST:TYPE", mapping, _RegistryStub(existing={"a", "b"}))
    fields = target["TEST:TYPE"].fields

    assert [(f.key, f.order, f.col, f.required) for f in fields] == [
        ("a", 1, 2, True),
        ("b", 3, 1, True),
    ]
