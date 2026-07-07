from __future__ import annotations

import argparse
import multiprocessing
import os
import signal
import time

from app import create_app
from app.ops.durable_queue import build_queue_from_app
from app.ops.task_handlers import TASK_HANDLERS


def _config_name() -> str:
    cfg = (os.environ.get("FLASK_CONFIG") or "").strip().lower()
    if cfg in {"development", "production", "default"}:
        return cfg
    env = (os.environ.get("FLASK_ENV") or os.environ.get("ENV") or "").strip().lower()
    return "production" if env in {"prod", "production"} else "development"


def _bootstrap_worker_runtime(app) -> None:
    from app.bootstrap import (
        bootstrap_deferred_sync,
        bootstrap_invoice_integration,
    )

    bootstrap_deferred_sync(app)
    bootstrap_invoice_integration(app)


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        value = default
    return max(1, value)


def _run_worker(queues: list[str], stop_event=None) -> None:
    if stop_event is not None:

        def _request_stop(_signum, _frame) -> None:
            stop_event.set()

        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)

    app = create_app(_config_name(), enable_bootstrap=False)
    _bootstrap_worker_runtime(app)
    with app.app_context():
        dq = build_queue_from_app(app)
        if stop_event is None:
            dq.worker_loop(TASK_HANDLERS, queues=queues)
        else:
            dq.worker_loop(TASK_HANDLERS, queues=queues, stop_event=stop_event)


def _shutdown_grace_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("DURABLE_QUEUE_SHUTDOWN_GRACE_SECONDS", "25")))
    except (TypeError, ValueError):
        return 25.0


def _run_worker_pool(queues: list[str], *, processes: int, stop_event=None) -> None:
    if stop_event is None:
        stop_event = multiprocessing.Event()

    children = [
        multiprocessing.Process(
            target=_run_worker,
            args=(queues, stop_event),
            name=f"durable-queue-worker-{idx + 1}",
        )
        for idx in range(processes)
    ]

    def _stop(_signum, _frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    for child in children:
        child.start()

    exit_code = 0
    try:
        while not stop_event.is_set():
            for child in children:
                child.join(timeout=1)
                if child.exitcode is not None:
                    exit_code = child.exitcode or 1
                    raise SystemExit(exit_code)
    finally:
        deadline = time.monotonic() + _shutdown_grace_seconds()
        for child in children:
            if not child.is_alive():
                continue
            remaining = max(0.0, deadline - time.monotonic())
            if remaining:
                child.join(timeout=remaining)
        for child in children:
            if child.is_alive():
                child.terminate()
        for child in children:
            child.join(timeout=10)
            if child.is_alive():
                child.kill()
    raise SystemExit(exit_code)


def main() -> None:
    parser = argparse.ArgumentParser(description="Durable Queue worker")
    parser.add_argument(
        "--queues", default="annuity,deferred", help="comma-separated queue names"
    )
    parser.add_argument(
        "--processes",
        type=int,
        default=_positive_int_env("DURABLE_QUEUE_WORKER_PROCESSES", 1),
        help="number of worker processes to run in this container",
    )
    args = parser.parse_args()

    queues = [q.strip() for q in args.queues.split(",") if q.strip()]
    processes = max(1, int(args.processes or 1))
    stop_event = multiprocessing.Event()

    def _request_stop(_signum, _frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    if processes == 1:
        _run_worker(queues, stop_event=stop_event)
        return
    _run_worker_pool(queues, processes=processes, stop_event=stop_event)


if __name__ == "__main__":
    main()
