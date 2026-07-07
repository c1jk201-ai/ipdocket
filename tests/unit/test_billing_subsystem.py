from flask import Flask

from app.services.billing.subsystem import (
    billing_subsystem_enabled,
    billing_subsystem_ready,
    get_billing_subsystem_state,
    initialize_billing_subsystem,
)


def test_initialize_billing_subsystem_marks_disabled_boundary_state() -> None:
    app = Flask(__name__)
    app.config.update(INVOICEAPP_INTEGRATED=False)

    state = initialize_billing_subsystem(app)

    assert state.enabled is False
    assert state.ready is False
    assert state.skipped_reason == "disabled"
    assert app.extensions["billing_subsystem"]["enabled"] is False
    assert billing_subsystem_enabled(app) is False
    assert billing_subsystem_ready(app) is True


def test_initialize_billing_subsystem_skips_connectivity_checks_in_testing() -> None:
    app = Flask(__name__)
    app.config.update(TESTING=True, INVOICEAPP_INTEGRATED=True)

    state = initialize_billing_subsystem(app)

    assert state.testing is True
    assert state.ready is True
    assert state.skipped_reason == "testing"
    assert billing_subsystem_enabled(app) is True
    assert get_billing_subsystem_state(app).ready is True
