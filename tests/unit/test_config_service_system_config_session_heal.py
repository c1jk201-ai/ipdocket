from __future__ import annotations


def test_config_service_system_config_heals_pending_rollback(app, db_session):
    from sqlalchemy.exc import IntegrityError

    from app.models.system_config import SystemConfig
    from app.models.user import User
    from app.services.core.config_service import ConfigService

    SystemConfig.set_config("TEST_CFG_KEY", "ok")
    db_session.commit()

    db_session.add(
        User(
            username="u1",
            email="dup@example.com",
            role="user",
            is_active=True,
        )
    )
    db_session.commit()

    db_session.add(
        User(
            username="u2",
            email="dup@example.com",
            role="user",
            is_active=True,
        )
    )
    try:
        db_session.commit()
    except IntegrityError:
        # Leave the session in a "pending rollback" state intentionally.
        pass

    assert bool(getattr(db_session, "is_active", False)) is False

    # Should heal and still be able to read config (via engine-based query).
    assert ConfigService.get_str("TEST_CFG_KEY") == "ok"

    assert bool(getattr(db_session, "is_active", False)) is True
