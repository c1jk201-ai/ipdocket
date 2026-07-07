from types import SimpleNamespace

import pytest

from app.utils import permissions


def _patch_invoice_roles(monkeypatch, invoice_roles_csv: str) -> None:
    def _get_str(key, default=None, **kwargs):
        if key == "STAFF_INVOICE_ROLES":
            return invoice_roles_csv
        return default

    monkeypatch.setattr(permissions.ConfigService, "get_str", _get_str)


def _user(role: str):
    return SimpleNamespace(is_authenticated=True, role=role)


def test_get_invoice_roles_always_includes_leadership_roles(monkeypatch):
    _patch_invoice_roles(monkeypatch, "admin,accounting")

    roles = permissions.get_invoice_roles()

    assert "accounting" in roles
    assert "mgmt_director" in roles
    assert "lead_attorney" in roles


@pytest.mark.parametrize("role", ["mgmt_director", "lead_attorney"])
def test_is_invoice_manager_allows_leadership_roles(monkeypatch, role):
    _patch_invoice_roles(monkeypatch, "admin,accounting")

    assert permissions.is_invoice_manager(_user(role)) is True


def test_is_invoice_manager_keeps_other_roles_config_driven(monkeypatch):
    _patch_invoice_roles(monkeypatch, "admin,accounting")

    assert permissions.is_invoice_manager(_user("partner_attorney")) is False
