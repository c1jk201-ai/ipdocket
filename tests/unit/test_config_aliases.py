from app.services.core.config_aliases import (
    apply_config_read_aliases,
    expand_config_delete_aliases,
    expand_config_update_aliases,
)
from app.models.system_config import SystemConfig
from app.services.core.config_service import ConfigService


def test_config_update_aliases_write_legacy_staff_role_key():
    expanded = expand_config_update_aliases(
        {"STAFF_PROFESSIONAL_ROLES": "lead_attorney,partner_attorney"}
    )

    assert expanded["STAFF_PROFESSIONAL_ROLES"] == "lead_attorney,partner_attorney"
    assert expanded["STAFF_ATTORNEY_ROLES"] == "lead_attorney,partner_attorney"


def test_config_update_aliases_preserve_explicit_alias_value():
    expanded = expand_config_update_aliases(
        {
            "STAFF_PROFESSIONAL_ROLES": "professional",
            "STAFF_ATTORNEY_ROLES": "legacy",
        }
    )

    assert expanded["STAFF_PROFESSIONAL_ROLES"] == "professional"
    assert expanded["STAFF_ATTORNEY_ROLES"] == "legacy"


def test_config_delete_aliases_include_staff_role_pair():
    assert expand_config_delete_aliases("STAFF_PROFESSIONAL_ROLES") == {
        "STAFF_PROFESSIONAL_ROLES",
        "STAFF_ATTORNEY_ROLES",
    }
    assert expand_config_delete_aliases(" STAFF_ATTORNEY_ROLES ") == {
        "STAFF_PROFESSIONAL_ROLES",
        "STAFF_ATTORNEY_ROLES",
    }


def test_config_read_aliases_mirror_missing_staff_role_value():
    data = {"STAFF_ATTORNEY_ROLES": "lead_attorney"}

    result = apply_config_read_aliases(data)

    assert result is data
    assert data["STAFF_ATTORNEY_ROLES"] == "lead_attorney"
    assert data["STAFF_PROFESSIONAL_ROLES"] == "lead_attorney"


def _config_value(key: str) -> str | None:
    row = SystemConfig.query.filter_by(key=key).first()
    return row.value if row else None


def test_admin_api_config_writes_staff_role_alias_pair(admin_client, db_session):
    response = admin_client.post(
        "/admin/api/config",
        json={"STAFF_PROFESSIONAL_ROLES": "lead_attorney"},
    )

    assert response.status_code == 200
    assert _config_value("STAFF_PROFESSIONAL_ROLES") == "lead_attorney"
    assert _config_value("STAFF_ATTORNEY_ROLES") == "lead_attorney"


def test_admin_api_config_reads_and_deletes_staff_role_alias_pair(admin_client, db_session):
    db_session.add(SystemConfig(key="STAFF_ATTORNEY_ROLES", value="partner_attorney"))
    db_session.commit()
    ConfigService.clear_cache()

    response = admin_client.get("/admin/api/config")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["STAFF_ATTORNEY_ROLES"] == "partner_attorney"
    assert payload["STAFF_PROFESSIONAL_ROLES"] == "partner_attorney"

    delete_response = admin_client.delete("/admin/api/config?key=STAFF_PROFESSIONAL_ROLES")

    assert delete_response.status_code == 200
    assert _config_value("STAFF_PROFESSIONAL_ROLES") is None
    assert _config_value("STAFF_ATTORNEY_ROLES") is None
