from flask import current_app

from app.extensions import db
from app.services.automation import automation_monitoring
from app.services.mail import foreign_email_pipeline


def test_review_rate_alert_does_not_force_draft_override(app):
    with app.app_context():
        assert automation_monitoring._desired_automation_override(["review_rate_high:0.81"]) == ""


def test_critical_alert_code_matches_metric_suffix(app):
    with app.app_context():
        assert (
            automation_monitoring._desired_automation_override(["error_rate_high:0.12"])
            == "HUMAN_REQUIRED"
        )


def test_draft_alerts_are_configurable(app, monkeypatch):
    with app.app_context():
        monkeypatch.setitem(
            current_app.config, "FOREIGN_EMAIL_DRIFT_DRAFT_ALERTS", "review_rate_high"
        )
        assert (
            automation_monitoring._desired_automation_override(["review_rate_high:0.81"])
            == "AUTO_DRAFT"
        )


def test_blank_level_override_suppresses_static_env_override(monkeypatch):
    monkeypatch.setattr(
        foreign_email_pipeline.ConfigService,
        "get_raw",
        staticmethod(lambda *_args, **_kwargs: ""),
    )
    monkeypatch.setattr(
        foreign_email_pipeline.ConfigService,
        "get_str",
        staticmethod(lambda *_args, **_kwargs: "AUTO_APPLY"),
    )

    assert foreign_email_pipeline._automation_level_cap() == "AUTO_APPLY"


def test_apply_override_clears_config_cache_when_value_changes(app, db_session, monkeypatch):
    cleared = []
    monkeypatch.setattr(
        automation_monitoring.ConfigService,
        "clear_cache",
        staticmethod(lambda: cleared.append(True)),
    )

    with app.app_context():
        automation_monitoring.SystemConfig.set_config(
            "FOREIGN_EMAIL_AUTOMATION_LEVEL_OVERRIDE", "AUTO_DRAFT"
        )
        db.session.commit()

        assert automation_monitoring._apply_automation_override(["review_rate_high:0.81"]) == ""
        assert (
            automation_monitoring.SystemConfig.get_config("FOREIGN_EMAIL_AUTOMATION_LEVEL_OVERRIDE")
            == ""
        )
        assert cleared == [True]


def test_string_false_disables_auto_downgrade(app, db_session, monkeypatch):
    with app.app_context():
        monkeypatch.setitem(
            current_app.config, "FOREIGN_EMAIL_DRIFT_AUTO_DOWNGRADE_ENABLED", "false"
        )
        automation_monitoring.SystemConfig.set_config("FOREIGN_EMAIL_AUTOMATION_LEVEL_OVERRIDE", "")
        db.session.commit()

        assert automation_monitoring._apply_automation_override(["error_rate_high:0.42"]) == ""
        assert (
            automation_monitoring.SystemConfig.get_config("FOREIGN_EMAIL_AUTOMATION_LEVEL_OVERRIDE")
            == ""
        )
