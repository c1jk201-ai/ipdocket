import logging
from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models.role import user_roles

logger = logging.getLogger(__name__)
# Role constants for calendar sync
ROLE_ADMIN = "admin"
ROLE_MGMT_DIRECTOR = "mgmt_director"  #  -  Deadline
ROLE_MGMT_STAFF = "mgmt_staff"  # Manager -  Deadline
ROLE_LEAD_ATTORNEY = "lead_attorney"  # table -  Task/Deadline
ROLE_PARTNER_ATTORNEY = "partner_attorney"  #  - Department( All) Task/Deadline
ROLE_PATENT_STAFF = "patent_staff"  # PatentContact -  Task/Deadline
ROLE_USER = "user"  # General User

ROLE_MGMT_ALIASES = {
    ROLE_MGMT_DIRECTOR,
    ROLE_MGMT_STAFF,
    "manager",
    "accounting",
}
ROLE_WORK_ALIASES = {
    ROLE_LEAD_ATTORNEY,
    ROLE_PARTNER_ATTORNEY,
    ROLE_PATENT_STAFF,
    "attorney",
    "patent_engineer",
    "paralegal",
    "staff",
}


_LEGACY_ROLE_MENU_PERMISSION_FALLBACKS = {
    ROLE_ADMIN: {
        "menu.cases",
        "menu.deadlines",
        "menu.notices",
        "menu.renewal",
        "menu.crm",
        "menu.accounting",
        "menu.statistics",
        "menu.mgmt",
        "menu.admin",
    },
    ROLE_MGMT_DIRECTOR: {
        "menu.cases",
        "menu.deadlines",
        "menu.notices",
        "menu.renewal",
        "menu.crm",
        "menu.accounting",
        "menu.statistics",
        "menu.mgmt",
    },
    ROLE_MGMT_STAFF: {
        "menu.cases",
        "menu.deadlines",
        "menu.notices",
        "menu.renewal",
        "menu.crm",
        "menu.accounting",
    },
    ROLE_LEAD_ATTORNEY: {
        "menu.cases",
        "menu.deadlines",
        "menu.notices",
        "menu.renewal",
        "menu.crm",
        "menu.accounting",
        "menu.statistics",
        "menu.mgmt",
    },
    ROLE_PARTNER_ATTORNEY: {
        "menu.cases",
        "menu.deadlines",
        "menu.notices",
        "menu.renewal",
        "menu.crm",
        "menu.accounting",
        "menu.statistics",
        "menu.mgmt",
    },
    ROLE_PATENT_STAFF: {
        "menu.cases",
        "menu.deadlines",
        "menu.notices",
        "menu.renewal",
        "menu.crm",
        "menu.accounting",
    },
    ROLE_USER: set(),
}


def _normalize_role_name(value: str | None) -> str:
    return (value or "").strip().lower()


def _split_legacy_role_csv(value: str | None) -> set[str]:
    raw = (value or "").strip()
    if not raw:
        return set()
    out = set()
    for part in raw.split(","):
        name = _normalize_role_name(part)
        if name:
            out.add(name)
    return out


def _legacy_role_has_menu_permission(role_names: set[str], permission_name: str) -> bool:
    for role_name in role_names:
        if permission_name in (_LEGACY_ROLE_MENU_PERMISSION_FALLBACKS.get(role_name) or set()):
            return True
    return False


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, index=True)
    email = db.Column(db.String(120), unique=True, index=True)
    display_name = db.Column(db.String(120))
    # Links the app login user to ipm staff directory (party/party_staff)
    staff_party_id = db.Column(db.Text, index=True)
    password_hash = db.Column(db.String(255))
    role = db.Column(db.String(20), default="user")  # admin, manager, user
    # RBAC Support: Multiple Roles
    roles = db.relationship(
        "Role",
        secondary=user_roles,
        lazy="subquery",
        back_populates="users",
    )

    department = db.Column(db.String(50))
    position = db.Column(db.String(50))
    is_active = db.Column(db.Boolean, default=True)
    menu_favorites = db.Column(db.Text, default="[]")  # JSON array of favorite menu IDs

    @property
    def is_admin(self) -> bool:
        # Compatibility: several admin gates historically referenced `user.is_admin`.
        return self.has_role(ROLE_ADMIN)

    @property
    def role_names(self) -> set[str]:
        names = {
            _normalize_role_name(getattr(role_obj, "name", None)) for role_obj in (self.roles or [])
        }
        names = {name for name in names if name}
        names.update(_split_legacy_role_csv(getattr(self, "role", None)))
        return names

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def can_view_all_mgmt_deadlines(self) -> bool:
        """Check if user can view all management deadlines ( Permissions)."""
        return self.has_role(ROLE_ADMIN) or self.has_role(ROLE_MGMT_DIRECTOR)

    def can_view_all_work_deadlines(self) -> bool:
        """Check if user can view all work/legal deadlines (table/ Permissions)."""
        return (
            self.has_role(ROLE_ADMIN)
            or self.has_role(ROLE_LEAD_ATTORNEY)
            or self.has_role(ROLE_PARTNER_ATTORNEY)
        )

    def is_mgmt_role(self) -> bool:
        """Check if user has a management role."""
        return bool(self.role_names.intersection(ROLE_MGMT_ALIASES))

    def is_work_role(self) -> bool:
        """Check if user has a work/patent role."""
        return bool(self.role_names.intersection(ROLE_WORK_ALIASES))

    # RBAC Methods
    def has_role(self, role_name: str) -> bool:
        """Check if user is assigned a specific role name."""
        target = _normalize_role_name(role_name)
        if not target:
            return False
        return target in self.role_names

    def has_permission(self, perm: str) -> bool:
        """
        Check if user has a specific permission.
        Admins automatically have all permissions.
        """
        if self.is_admin:
            return True

        normalized_perm = (perm or "").strip()
        if not normalized_perm:
            return False

        for role_obj in self.roles or []:
            if role_obj.has_permission(normalized_perm):
                return True

        # Backward compatibility:
        # - users may still rely on users.role without user_roles mapping.
        # - legacy role defaults (menu permissions) should keep working.
        role_names = self.role_names
        if role_names and not (self.roles or []):
            try:
                from app.models.role import Role

                db_roles = Role.query.filter(Role.name.in_(sorted(role_names))).all()
                for role_obj in db_roles:
                    if role_obj.has_permission(normalized_perm):
                        return True
            except Exception:
                # Fall through to static legacy defaults.
                logger.debug(
                    "Role.query fallback lookup failed for user_id=%s",
                    getattr(self, "id", None),
                    exc_info=True,
                )

        return _legacy_role_has_menu_permission(role_names, normalized_perm)

    def __repr__(self):
        return f"<User {self.username}>"


