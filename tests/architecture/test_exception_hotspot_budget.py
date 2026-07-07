from __future__ import annotations

import ast
from pathlib import Path

HOTSPOT_BUDGETS = {
    Path("app/blueprints/admin/routes.py"): 6,
    Path("app/blueprints/api/routes.py"): 4,
    Path("app/blueprints/statistics/routes.py"): 16,
}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")


def _count_broad_exception_handlers(path: Path) -> int:
    tree = ast.parse(_read_text(path), filename=str(path))
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            if isinstance(handler.type, ast.Name) and handler.type.id == "Exception":
                count += 1
    return count


def test_exception_hotspots_do_not_regress() -> None:
    root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    total = 0

    for rel_path, budget in HOTSPOT_BUDGETS.items():
        count = _count_broad_exception_handlers(root / rel_path)
        total += count
        if count > budget:
            offenders.append(f"{rel_path}: {count} > {budget}")

    assert not offenders, "Broad exception hotspot budgets regressed:\n" + "\n".join(offenders)
    assert total <= sum(HOTSPOT_BUDGETS.values())
