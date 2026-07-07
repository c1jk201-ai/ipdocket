from types import SimpleNamespace

from flask import Flask


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConnection:
    def __init__(self, backend_pid: int, *, fail_on_pid_call: int | None = None):
        self.backend_pid = backend_pid
        self.fail_on_pid_call = fail_on_pid_call
        self.pid_calls = 0
        self.closed = False
        self.execution_options_calls: list[dict[str, object]] = []

    def execution_options(self, **kwargs):
        self.execution_options_calls.append(kwargs)
        return self

    def execute(self, statement, params=None):
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            return _FakeResult(True)
        if "pg_advisory_unlock" in sql:
            return _FakeResult(True)
        if "pg_backend_pid" in sql:
            self.pid_calls += 1
            if self.fail_on_pid_call and self.pid_calls >= self.fail_on_pid_call:
                raise RuntimeError("server closed the connection unexpectedly")
            return _FakeResult(self.backend_pid)
        if "SELECT 1" in sql:
            return _FakeResult(1)
        raise AssertionError(f"Unexpected SQL in fake connection: {sql}")

    def close(self):
        self.closed = True


class _FakeEngine:
    def __init__(self, connections: list[_FakeConnection]):
        self.dialect = SimpleNamespace(name="postgresql")
        self._connections = list(connections)

    def connect(self):
        if not self._connections:
            raise RuntimeError("no fake connections left")
        return self._connections.pop(0)


class _FakeScheduler:
    def __init__(self, *args, **kwargs):
        self.jobs: dict[str, dict[str, object]] = {}
        self.started = False
        self.shutdown_called = False

    def add_job(self, func, *args, **kwargs):
        job_id = kwargs.get("id") or f"job_{len(self.jobs)}"
        self.jobs[job_id] = {"func": func, "args": args, "kwargs": kwargs}

    def start(self):
        self.started = True

    def shutdown(self, wait=False):  # noqa: ARG002
        self.shutdown_called = True


class _FakeFileLock:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _make_test_app():
    app = Flask(__name__)
    app.config.update(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "SCHEDULER_ENABLED": True,
            "RUN_SCHEDULER": True,
            "BASE_DIR": "/tmp",
        }
    )
    return app


def test_scheduler_lock_health_recovers_lost_connection(monkeypatch):
    from apscheduler.schedulers import background as aps_bg

    from app.services.ops import scheduler as scheduler_mod

    app = _make_test_app()
    first_conn = _FakeConnection(backend_pid=111, fail_on_pid_call=2)
    startup_conn = _FakeConnection(backend_pid=999)
    second_conn = _FakeConnection(backend_pid=222)
    fake_engine = _FakeEngine([first_conn, startup_conn, second_conn])
    fake_db = SimpleNamespace(engine=fake_engine, session=SimpleNamespace())

    app.extensions.pop("apscheduler", None)
    app.extensions.pop("apscheduler_lock_conn", None)
    app.extensions.pop("apscheduler_lock_backend_pid", None)

    app.config["SCHEDULER_ENABLED"] = True
    app.config["RUN_SCHEDULER"] = True
    app.config["SCHEDULER_LOCK_HEALTHCHECK_SECONDS"] = 60
    app.config["SCHEDULER_EXIT_ON_LOCK_LOSS"] = True

    monkeypatch.setattr(aps_bg, "BackgroundScheduler", _FakeScheduler)
    monkeypatch.setattr(scheduler_mod, "db", fake_db)
    monkeypatch.setattr(scheduler_mod.atexit, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scheduler_mod, "_record_scheduler_startup_heartbeat", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        scheduler_mod.os,
        "_exit",
        lambda code: (_ for _ in ()).throw(AssertionError(f"os._exit called with {code}")),
    )

    with app.app_context():
        scheduler = scheduler_mod.init_app(app)

    assert scheduler is not None
    assert scheduler.started is True
    assert first_conn.execution_options_calls == [{"isolation_level": "AUTOCOMMIT"}]

    health_job = scheduler.jobs["scheduler_lock_health"]["func"]
    health_job()

    assert scheduler.shutdown_called is False
    assert app.extensions["apscheduler_lock_conn"] is second_conn
    assert app.extensions["apscheduler_lock_backend_pid"] == 222
    assert first_conn.closed is True
    assert second_conn.execution_options_calls == [{"isolation_level": "AUTOCOMMIT"}]


def test_register_job_wraps_runner_in_app_context_and_cleans_up(monkeypatch):
    from flask import current_app

    from app.services.ops import scheduler as scheduler_mod

    app = _make_test_app()
    scheduler = _FakeScheduler()
    backoff = object()
    calls: list[tuple[object, ...]] = []

    def _fake_run_with_job_log(app_arg, backoff_arg, job_name, runner):
        calls.append(
            (
                "run_with_job_log",
                app_arg is app,
                backoff_arg is backoff,
                job_name,
                current_app._get_current_object() is app,
            )
        )
        calls.append(("runner_result", runner()))

    monkeypatch.setattr(scheduler_mod, "_run_with_job_log", _fake_run_with_job_log)
    monkeypatch.setattr(
        scheduler_mod,
        "_cleanup_db_session",
        lambda job_name: calls.append(("cleanup", job_name)),
    )

    spec = scheduler_mod._SchedulerJobSpec(
        id="test_job",
        name="Test Job",
        job_name="wrapped_job",
        runner=lambda app_arg: calls.append(
            ("runner", app_arg is app, current_app._get_current_object() is app)
        )
        or {"ok": True},
        trigger_factory=lambda _app, imports: imports.interval_trigger_cls(seconds=15),
        scheduler_kwargs={"max_instances": 2},
    )

    scheduler_imports = SimpleNamespace(interval_trigger_cls=lambda **kwargs: ("interval", kwargs))

    assert scheduler_mod.register_job(app, scheduler, scheduler_imports, backoff, spec) is True

    job = scheduler.jobs["test_job"]
    assert job["args"] == (("interval", {"seconds": 15}),)
    assert job["kwargs"]["name"] == "Test Job"
    assert job["kwargs"]["max_instances"] == 2

    job["func"]()

    assert calls == [
        ("run_with_job_log", True, True, "wrapped_job", True),
        ("runner", True, True),
        ("runner_result", {"ok": True}),
        ("cleanup", "wrapped_job"),
    ]


def test_startup_heartbeat_records_scheduler_heartbeat(monkeypatch):
    from flask import current_app

    from app.services.ops import scheduler as scheduler_mod

    app = _make_test_app()
    backoff = object()
    calls: list[tuple[object, ...]] = []

    def _fake_run_with_job_log(app_arg, backoff_arg, job_name, runner):
        result = runner()
        calls.append(
            (
                "run_with_job_log",
                app_arg is app,
                backoff_arg is backoff,
                job_name,
                current_app._get_current_object() is app,
                result.get("ok"),
                "pid" in result,
            )
        )

    monkeypatch.setattr(scheduler_mod, "_run_with_job_log", _fake_run_with_job_log)
    monkeypatch.setattr(
        scheduler_mod,
        "_cleanup_db_session",
        lambda job_name: calls.append(("cleanup", job_name)),
    )

    scheduler_mod._record_scheduler_startup_heartbeat(app, backoff)

    assert calls == [
        ("run_with_job_log", True, True, "scheduler_heartbeat", True, True, True),
        ("cleanup", "scheduler_heartbeat"),
    ]


def test_init_app_registers_all_enabled_job_specs(monkeypatch):
    from apscheduler.schedulers import background as aps_bg

    from app.services.ops import scheduler as scheduler_mod

    app = _make_test_app()
    fake_db = SimpleNamespace(
        engine=SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
        session=SimpleNamespace(),
    )

    app.extensions.pop("apscheduler", None)
    app.extensions.pop("apscheduler_lock_conn", None)
    app.extensions.pop("apscheduler_lock_backend_pid", None)
    app.extensions.pop("apscheduler_lock_file", None)

    app.config.update(
        {
            "SCHEDULER_ENABLED": True,
            "RUN_SCHEDULER": True,
            "DEADLINE_AUTO_CLOSE_ENABLED": True,
            "OFFICE_ACTION_AUTO_CLOSE_ENABLED": True,
            "WORKLOG_AUTO_BACKFILL_FROM_DOCKETS_ENABLED": False,
            "HOUSEKEEPING_ENABLED": True,
            "ERROR_REPORT_ALERTS_ENABLED": False,
            "EMAIL_INGESTION_ENABLED": False,
            "FOREIGN_EMAIL_DRIFT_MONITOR_ENABLED": True,
            "DISK_MONITOR_ENABLED": True,
            "MATTER_STATUS_RECALC_QUEUE_ENABLED": True,
            "MATTER_STATUS_CACHE_AUDIT_ENABLED": True,
            "MATTER_STATUS_CACHE_RECONCILE_ENABLED": False,
        }
    )

    monkeypatch.setattr(aps_bg, "BackgroundScheduler", _FakeScheduler)
    monkeypatch.setattr(scheduler_mod, "db", fake_db)
    monkeypatch.setattr(scheduler_mod, "_acquire_file_lock", lambda _path: _FakeFileLock())
    monkeypatch.setattr(scheduler_mod.atexit, "register", lambda *_args, **_kwargs: None)
    startup_heartbeat_calls = []
    monkeypatch.setattr(
        scheduler_mod,
        "_record_scheduler_startup_heartbeat",
        lambda app_arg, backoff_arg: startup_heartbeat_calls.append(
            (app_arg is app, hasattr(backoff_arg, "should_skip"))
        ),
    )

    with app.app_context():
        scheduler = scheduler_mod.init_app(app)
        expected_ids = {spec.id for spec in scheduler_mod._JOB_SPECS if spec.enabled(app)}

    assert scheduler is not None
    assert scheduler.started is True
    assert startup_heartbeat_calls == [(True, True)]
    assert expected_ids <= set(scheduler.jobs)
    assert "scheduler_lock_health" not in scheduler.jobs
    assert "worklog_docket_backfill" not in scheduler.jobs
    assert "email_ingestion" not in scheduler.jobs
