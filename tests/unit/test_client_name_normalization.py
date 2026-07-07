from __future__ import annotations

from app.services.client.name_normalization import normalize_client_name


def test_normalize_client_name_preserves_ascii_name():
    result = normalize_client_name("  Acme IP LLC  ")

    assert result == {
        "name": "Acme IP LLC",
        "client_name": "Acme IP LLC",
        "name_en": "Acme IP LLC",
    }


def test_normalize_client_name_collapses_blank_name():
    result = normalize_client_name("   ")

    assert result == {"name": "", "client_name": "", "name_en": ""}


def test_normalize_client_name_leaves_non_alpha_without_english_alias():
    result = normalize_client_name("12345")

    assert result == {"name": "12345", "client_name": "12345", "name_en": ""}
