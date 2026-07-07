import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

REMOVED_RUNTIME_BRIDGES = {
    Path("app/services/billing/billing_runtime.py"),
    Path("app/services/case/case_runtime.py"),
    Path("app/services/ops/backup_runtime.py"),
}

TEMPORARY_ALLOWED_BLUEPRINT_IMPORTS = {
    Path("app/services/case/profile_syncs.py"): {"app.blueprints.case.helpers"},
    Path("app/services/client/client_merge_service.py"): {
        "app.blueprints.billing_invoices.routes.admin"
    },
    Path("app/services/deletion_manager.py"): {"app.blueprints.billing_invoices.auth"},
    Path("app/services/matter/matter_use_cases.py"): {"app.blueprints.case.helpers"},
    Path("app/services/ops/backup_service.py"): {"app.blueprints.billing_invoices.routes.admin"},
    Path("app/services/ops/housekeeping.py"): {"app.blueprints.billing_invoices.routes.admin"},
}


def _iter_service_files(root: Path) -> list[Path]:
    return sorted(path for path in (root / "app" / "services").rglob("*.py"))


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")


def _collect_blueprint_imports(root: Path, path: Path) -> list[str]:
    tree = ast.parse(_read_text(path), filename=str(path))
    rel = path.relative_to(root)
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app.blueprints"):
                    hits.append(f"{rel}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("app.blueprints"):
                hits.append(f"{rel}:{node.lineno} from {module}")
    return hits


def test_runtime_bridge_modules_are_removed() -> None:
    leftovers = [
        str(rel_path) for rel_path in REMOVED_RUNTIME_BRIDGES if (ROOT / rel_path).exists()
    ]
    assert not leftovers, "Runtime bridge modules must stay removed:\n" + "\n".join(leftovers)


def test_service_blueprint_imports_stay_contained() -> None:
    offenders: list[str] = []
    for path in _iter_service_files(ROOT):
        rel = path.relative_to(ROOT)
        allowed = TEMPORARY_ALLOWED_BLUEPRINT_IMPORTS.get(rel, set())
        for hit in _collect_blueprint_imports(ROOT, path):
            if not any(hit.endswith(f" {module}") for module in allowed):
                offenders.append(hit)

    assert not offenders, "Unexpected blueprint imports remain in app/services:\n" + "\n".join(
        offenders
    )
