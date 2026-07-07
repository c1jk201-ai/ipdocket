from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SHARED_HELPER_MODULES = [
    Path("app/services/case/helpers_staff.py"),
    Path("app/services/case/helpers_files.py"),
    Path("app/services/case/form_support.py"),
    Path("app/services/case/staff_context.py"),
    Path("app/services/case/profile_syncs.py"),
    Path("app/services/matter/auto_status_apply.py"),
]

BROAD_EXCEPTION_BUDGETS = {
    Path("app/services/case/helpers_staff.py"): 6,
    Path("app/services/case/helpers_files.py"): 10,
    Path("app/services/case/form_support.py"): 6,
    Path("app/services/case/staff_context.py"): 3,
    Path("app/services/case/profile_syncs.py"): 0,
    Path("app/services/matter/auto_status_apply.py"): 5,
}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")


def _parse(path: Path) -> ast.AST:
    return ast.parse(_read_text(path), filename=str(path))


def _attribute_chain(node: ast.AST) -> str:
    parts: list[str] = []
    cursor = node
    while isinstance(cursor, ast.Attribute):
        parts.append(cursor.attr)
        cursor = cursor.value
    if isinstance(cursor, ast.Name):
        parts.append(cursor.id)
    return ".".join(reversed(parts))


def _collect_tx_boundary_calls(path: Path) -> list[str]:
    rel = path.relative_to(ROOT)
    hits: list[str] = []
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in {"commit", "rollback"}:
            continue
        hits.append(f"{rel}:{node.lineno} {_attribute_chain(func)}")
    return hits


def _collect_current_app_config_accesses(path: Path) -> list[str]:
    rel = path.relative_to(ROOT)
    hits: list[str] = []
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Attribute) and _attribute_chain(node) == "current_app.config":
            hits.append(f"{rel}:{node.lineno} current_app.config")
    return hits


def _count_broad_exception_handlers(path: Path) -> int:
    count = 0
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            if isinstance(handler.type, ast.Name) and handler.type.id == "Exception":
                count += 1
    return count


def test_shared_helper_modules_do_not_define_transaction_boundaries() -> None:
    offenders: list[str] = []
    for rel_path in SHARED_HELPER_MODULES:
        offenders.extend(_collect_tx_boundary_calls(ROOT / rel_path))
    assert (
        not offenders
    ), "Shared helper modules must not commit or rollback directly:\n" + "\n".join(offenders)


def test_shared_helper_modules_do_not_read_current_app_config_directly() -> None:
    offenders: list[str] = []
    for rel_path in SHARED_HELPER_MODULES:
        offenders.extend(_collect_current_app_config_accesses(ROOT / rel_path))
    assert (
        not offenders
    ), "Shared helper modules must use ConfigService or explicit args for config:\n" + "\n".join(
        offenders
    )


def test_shared_helper_exception_budgets_do_not_regress() -> None:
    offenders: list[str] = []
    total = 0
    for rel_path, budget in BROAD_EXCEPTION_BUDGETS.items():
        count = _count_broad_exception_handlers(ROOT / rel_path)
        total += count
        if count > budget:
            offenders.append(f"{rel_path}: {count} > {budget}")
    assert not offenders, "Shared helper broad exception budgets regressed:\n" + "\n".join(
        offenders
    )
    assert total <= sum(BROAD_EXCEPTION_BUDGETS.values())
