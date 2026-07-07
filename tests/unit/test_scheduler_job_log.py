import json
from datetime import datetime

from app.extensions import db
from app.models.job_run import JobRun
from app.services.ops import scheduler as scheduler_mod


class _Backoff:
    def __init__(self):
        self.successes: list[str] = []

    def should_skip(self, job_name: str, *, now: datetime):  # noqa: ARG002
        return False, None, 0

    def record_success(self, job_name: str) -> None:
        self.successes.append(job_name)

    def record_failure(self, job_name: str, *, now: datetime):  # noqa: ARG002
        return 1, 60, now


def test_run_with_job_log_finishes_after_runner_removes_scoped_session(app, db_session):
    with app.app_context():
        backoff = _Backoff()

        def _runner():
            db.session.rollback()
            db.session.remove()
            return {"ok": True}

        scheduler_mod._run_with_job_log(app, backoff, "unit_session_reset_job", _runner)

        db.session.remove()
        row = JobRun.query.filter_by(job_name="unit_session_reset_job").one()
        assert row.status == "success"
        assert row.finished_at is not None
        assert json.loads(row.output_ref or "{}") == {"ok": True}
        assert backoff.successes == ["unit_session_reset_job"]


def test_job_log_rollback_invalidates_failed_session(monkeypatch):
    calls: list[object] = []

    class _Session:
        def rollback(self):
            calls.append("rollback")
            raise RuntimeError("rollback failed")

        def invalidate(self):
            calls.append("invalidate")

    monkeypatch.setattr(
        scheduler_mod,
        "report_swallowed_exception",
        lambda exc, **kwargs: calls.append((type(exc).__name__, kwargs.get("context"))),
    )

    scheduler_mod._rollback_job_log_session(_Session(), context="unit")

    assert calls == [
        "rollback",
        "invalidate",
        ("RuntimeError", "unit.rollback"),
    ]
