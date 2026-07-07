import pytest

from app.ops.task_handlers import TASK_HANDLER_SPECS, TASK_HANDLERS, _load_handler_function


def test_durable_task_handler_registry_matches_specs() -> None:
    assert set(TASK_HANDLERS) == {spec.task for spec in TASK_HANDLER_SPECS}


@pytest.mark.parametrize("spec", TASK_HANDLER_SPECS, ids=lambda spec: spec.task)
def test_durable_task_handler_targets_are_importable(spec) -> None:
    target = _load_handler_function(spec.module_path, spec.func_name)

    assert callable(target)
