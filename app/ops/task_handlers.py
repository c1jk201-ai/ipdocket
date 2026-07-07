from __future__ import annotations

import importlib
import inspect
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Callable, cast


@dataclass(frozen=True)
class TaskHandlerSpec:
    task: str
    module_path: str
    func_name: str


def _load_handler_function(module_path: str, func_name: str) -> Callable[..., Any]:
    mod = importlib.import_module(module_path)
    fn = getattr(mod, func_name)
    if not callable(fn):
        raise TypeError(f"{module_path}.{func_name} is not callable")
    return cast(Callable[..., Any], fn)


def _lazy_call(module_path: str, func_name: str, payload: dict[str, Any]) -> None:
    """
    Lazy import wrapper to invoke domain service logic.

    Supports both "kwargs-style" handlers and "payload-dict" handlers.
    """
    fn = _load_handler_function(module_path, func_name)
    payload = payload or {}

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        # Fallback: assume kwargs-style
        fn(**payload)
        return

    params = list(sig.parameters.values())
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
        fn(**payload)
        return

    normal = [
        p
        for p in params
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    ]

    if not normal:
        fn()
        return

    if len(normal) == 1:
        p = normal[0]
        if p.kind == inspect.Parameter.KEYWORD_ONLY:
            if p.name in payload:
                fn(**{p.name: payload[p.name]})
            else:
                fn()
            return

        if p.name in payload:
            fn(**{p.name: payload[p.name]})
            return

        fn(payload)
        return

    kwargs = {k: v for k, v in payload.items() if k in sig.parameters}
    fn(**kwargs)


TASK_HANDLER_SPECS: tuple[TaskHandlerSpec, ...] = (
    TaskHandlerSpec(
        "annuity.run",
        "app.services.annuity.annuity_service",
        "ensure_annuities_for_all_registered_matters",
    ),
    TaskHandlerSpec(
        "annuity.workflow_sync",
        "app.services.annuity.annuity_sync_queue",
        "run_annuity_workflow_sync_task",
    ),
    TaskHandlerSpec(
        "deferred.sync",
        "app.services.workflow.deferred_task_executor",
        "run_deferred_sync_task",
    ),
    TaskHandlerSpec(
        "file_asset.virus_scan",
        "app.services.storage.file_asset_scan_queue",
        "run_file_asset_virus_scan",
    ),
    TaskHandlerSpec(
        "workflow.delete",
        "app.services.workflow.deletion_jobs",
        "run_delete_workflow_job",
    ),
    TaskHandlerSpec(
        "client.crm_post_save",
        "app.services.client.background_jobs",
        "run_crm_client_post_save",
    ),
    TaskHandlerSpec(
        "client.invoice_post_save",
        "app.services.client.background_jobs",
        "run_invoice_client_post_save",
    ),
    TaskHandlerSpec(
        "matter_status.recalc",
        "app.services.matter.matter_status_recalc_queue",
        "run_matter_status_recalc_task",
    ),
)


def _build_task_handler(spec: TaskHandlerSpec) -> Callable[[dict[str, Any]], None]:
    def _handler(payload: dict[str, Any]) -> None:
        _lazy_call(spec.module_path, spec.func_name, payload)

    return _handler


def validate_task_handler_specs(
    specs: Iterable[TaskHandlerSpec] = TASK_HANDLER_SPECS,
) -> None:
    for spec in specs:
        _load_handler_function(spec.module_path, spec.func_name)


TASK_HANDLERS: dict[str, Callable[[dict[str, Any]], None]] = {
    spec.task: _build_task_handler(spec) for spec in TASK_HANDLER_SPECS
}
