from app.models.permissions import Permissions
from app.security.default_role_profiles import get_default_role_profiles


def test_mgmt_staff_default_role_can_edit_all_cases():
    profiles = get_default_role_profiles()

    assert Permissions.CASE_VIEW_ALL in profiles["mgmt_staff"]
    assert Permissions.CASE_EDIT_ALL in profiles["mgmt_staff"]
    assert Permissions.CASE_ASSIGN_ALL in profiles["mgmt_staff"]
