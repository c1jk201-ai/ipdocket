from app.extensions import db

# Association Table for User <-> Role (Many-to-Many)
user_roles = db.Table(
    "user_roles",
    db.Column(
        "user_id", db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    ),
    db.Column(
        "role_id", db.Integer, db.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    ),
)


class Role(db.Model):
    __tablename__ = "roles"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, index=True, nullable=False)
    description = db.Column(db.String(200))
    # JSON array storing permission keys like ["menu.cases", "menu.accounting", "menu.admin"]
    permissions = db.Column(db.JSON, default=list)

    users = db.relationship(
        "User",
        secondary=user_roles,
        lazy="subquery",
        back_populates="roles",
    )

    def __repr__(self):
        return f"<Role {self.name}>"

    def has_permission(self, perm: str) -> bool:
        """Check if this role has the specific permission."""
        return perm in (self.permissions or [])
