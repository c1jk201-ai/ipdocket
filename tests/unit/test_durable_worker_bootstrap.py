from __future__ import annotations

import sys

import pytest

from app.ops import worker


class _AppContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _App:
    def app_context(self):
        return _AppContext()


def test_worker_uses_targeted_bootstrap(monkeypatch):
    app = _App()
    calls: list[tuple] = []

    def _fake_create_app(config_name, *, enable_bootstrap=True, **kwargs):
        calls.append(("create_app", config_name, enable_bootstrap))
        return app

    def _fake_bootstrap(runtime_app):
        calls.append(("bootstrap_worker_runtime", runtime_app))

    class _Queue:
        def worker_loop(self, handlers, *, queues, stop_event=None):
            calls.append(("worker_loop", handlers, queues))
            raise SystemExit(0)

    monkeypatch.setattr(sys, "argv", ["worker", "--queues", "email,deferred"])
    monkeypatch.setattr(worker, "create_app", _fake_create_app)
    monkeypatch.setattr(worker, "_bootstrap_worker_runtime", _fake_bootstrap)
    monkeypatch.setattr(worker, "build_queue_from_app", lambda runtime_app: _Queue())

    with pytest.raises(SystemExit):
        worker.main()

    assert calls[0] == ("create_app", "development", False)
    assert calls[1] == ("bootstrap_worker_runtime", app)
    assert calls[2][0] == "worker_loop"
    assert calls[2][2] == ["email", "deferred"]
