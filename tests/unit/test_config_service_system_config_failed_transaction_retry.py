from __future__ import annotations


def test_config_service_system_config_rolls_back_and_retries_on_failed_transaction(
    app, monkeypatch
):
    from sqlalchemy.exc import DBAPIError

    from app.extensions import db
    from app.services.core.config_service import ConfigService

    class DummyOrig(Exception):
        pgcode = "25P02"

    calls = {"execute": 0, "rollback": 0}

    def fake_execute(*args, **kwargs):
        calls["execute"] += 1
        if calls["execute"] == 1:
            raise DBAPIError(
                "SELECT value FROM system_config WHERE key = :key",
                {"key": "CASE_STRICT_DATE_VALIDATION"},
                DummyOrig("current transaction is aborted"),
                connection_invalidated=False,
            )

        class _Result:
            def scalar(self):
                return "1"

        return _Result()

    def fake_rollback():
        calls["rollback"] += 1

    with app.app_context():
        monkeypatch.setattr(db.session, "execute", fake_execute)
        monkeypatch.setattr(db.session, "rollback", fake_rollback)

        assert ConfigService.get_bool("CASE_STRICT_DATE_VALIDATION", default=False) is True

    assert calls["execute"] == 2
    assert calls["rollback"] == 1


def test_config_service_system_config_rolls_back_and_retries_on_invalidated_connection(
    app, monkeypatch
):
    from sqlalchemy.exc import DBAPIError

    from app.extensions import db
    from app.services.core.config_service import ConfigService

    class DummyOrig(Exception):
        pass

    calls = {"execute": 0, "rollback": 0}

    def fake_execute(*args, **kwargs):
        calls["execute"] += 1
        if calls["execute"] == 1:
            raise DBAPIError(
                "SELECT value FROM system_config WHERE key = :key",
                {"key": "FOREIGN_EMAIL_AUTOMATION_LEVEL_OVERRIDE"},
                DummyOrig("server closed the connection unexpectedly"),
                connection_invalidated=True,
            )

        class _Result:
            def scalar(self):
                return "AUTO_DRAFT"

        return _Result()

    def fake_rollback():
        calls["rollback"] += 1

    with app.app_context():
        ConfigService.clear_cache()
        monkeypatch.setattr(db.session, "execute", fake_execute)
        monkeypatch.setattr(db.session, "rollback", fake_rollback)

        assert (
            ConfigService.get_str(
                "FOREIGN_EMAIL_AUTOMATION_LEVEL_OVERRIDE",
                default="",
                allow_blank=False,
            )
            == "AUTO_DRAFT"
        )

    assert calls["execute"] == 2
    assert calls["rollback"] == 1


def test_config_service_system_config_retries_on_committed_state_invalid_request(app, monkeypatch):
    from sqlalchemy.exc import InvalidRequestError

    from app.extensions import db
    from app.services.core.config_service import ConfigService

    calls = {"execute": 0, "rollback": 0}

    def fake_execute(*args, **kwargs):
        calls["execute"] += 1
        if calls["execute"] == 1:
            raise InvalidRequestError(
                "This session is in 'committed' state; no further SQL can be emitted within this transaction."
            )

        class _Result:
            def scalar(self):
                return "1"

        return _Result()

    def fake_rollback():
        calls["rollback"] += 1

    with app.app_context():
        ConfigService.clear_cache()
        monkeypatch.setattr(db.session, "execute", fake_execute)
        monkeypatch.setattr(db.session, "rollback", fake_rollback)

        assert ConfigService.get_bool("CASE_STRICT_DATE_VALIDATION", default=False) is True

    assert calls["execute"] == 2
    assert calls["rollback"] == 1
