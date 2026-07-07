import pytest


def test_background_service_does_not_run_sync_in_request_when_not_initialized(app, monkeypatch):
    from app.services.ops.background import BackgroundService

    ran = {"value": False}

    def _job():
        ran["value"] = True

    # Simulate production-like behavior for this test.
    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setitem(app.config, "BACKGROUND_ALLOW_SYNC_FALLBACK_IN_REQUEST", False)
    monkeypatch.setattr(app, "debug", False, raising=False)

    BackgroundService.shutdown()
    monkeypatch.setattr(
        BackgroundService,
        "init_app",
        lambda _app: (_ for _ in ()).throw(RuntimeError("init failed")),
    )

    with app.test_request_context("/"):
        BackgroundService.run_async(_job)

    assert ran["value"] is False


def test_background_service_does_not_run_sync_in_request_when_submit_fails(app, monkeypatch):
    from app.services.ops.background import BackgroundService

    ran = {"value": False}

    def _job():
        ran["value"] = True

    class _FailingRunner:
        def submit(self, *args, **kwargs):
            raise RuntimeError("submit failed")

        def shutdown(self) -> None:
            return None

    # Simulate production-like behavior for this test.
    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setitem(app.config, "BACKGROUND_ALLOW_SYNC_FALLBACK_IN_REQUEST", False)
    monkeypatch.setattr(app, "debug", False, raising=False)

    BackgroundService.set_runner(_FailingRunner())

    with app.test_request_context("/"):
        BackgroundService.run_async(_job)

    assert ran["value"] is False


def test_background_service_can_fallback_to_sync_outside_request(app, monkeypatch):
    from app.services.ops.background import BackgroundService

    ran = {"value": False}

    def _job():
        ran["value"] = True

    # Even if the threadpool isn't available, CLI/app-context tasks should still run.
    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setitem(app.config, "BACKGROUND_ALLOW_SYNC_FALLBACK_IN_REQUEST", False)
    monkeypatch.setattr(app, "debug", False, raising=False)

    BackgroundService.shutdown()
    monkeypatch.setattr(
        BackgroundService,
        "init_app",
        lambda _app: (_ for _ in ()).throw(RuntimeError("init failed")),
    )

    # Run in a fresh thread to avoid leaked request contexts from other tests.
    import threading

    def _worker():
        with app.app_context():
            BackgroundService.run_async(_job)

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=5)

    assert ran["value"] is True


def test_background_service_runs_critical_detached_when_not_initialized_in_request(
    app, monkeypatch
):
    import threading

    from app.services.ops.background import BackgroundService

    done = threading.Event()

    def _job():
        done.set()

    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setitem(app.config, "BACKGROUND_ALLOW_SYNC_FALLBACK_IN_REQUEST", False)
    monkeypatch.setattr(app, "debug", False, raising=False)

    BackgroundService.shutdown()
    monkeypatch.setattr(
        BackgroundService,
        "init_app",
        lambda _app: (_ for _ in ()).throw(RuntimeError("init failed")),
    )

    with app.test_request_context("/"):
        BackgroundService.run_async(_job, _critical=True, _context="unit.test")

    assert done.wait(timeout=2.0) is True


def test_background_service_runs_critical_detached_when_submit_fails_in_request(app, monkeypatch):
    import threading

    from app.services.ops.background import BackgroundService

    done = threading.Event()

    def _job():
        done.set()

    class _FailingRunner:
        def submit(self, *args, **kwargs):
            raise RuntimeError("submit failed")

        def shutdown(self) -> None:
            return None

    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setitem(app.config, "BACKGROUND_ALLOW_SYNC_FALLBACK_IN_REQUEST", False)
    monkeypatch.setattr(app, "debug", False, raising=False)

    BackgroundService.set_runner(_FailingRunner())

    with app.test_request_context("/"):
        BackgroundService.run_async(_job, _critical=True, _context="unit.test")

    assert done.wait(timeout=2.0) is True
