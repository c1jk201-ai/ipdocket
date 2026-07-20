from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_SOURCE_FILES = (
    "app/data/canonical_fields_extended.csv",
    "app/data/case_field_mappings.json",
    "app/data/case_parameter_mapping.csv",
    "app/data/task_distribution_rules.json",
    "app/data/unified_field_registry.json",
    "app/services/uploads/__init__.py",
    "app/services/uploads/dto.py",
    "app/services/uploads/intake_security.py",
    "app/services/uploads/upload_session_service.py",
    "app/services/uploads/upload_validation.py",
    "app/services/uploads/zip_safety.py",
    "app/services/uspto/uspto_form_parser.py",
    "app/services/uspto/uspto_practice.py",
)

ROOT_RUNTIME_DIRECTORIES = (
    "data",
    "uploads",
    "logs",
    "backups",
    "reports",
    "instance",
    "output",
    "transfer",
    "legacy_data",
)


def _patterns(filename: str) -> list[str]:
    return [
        line.strip()
        for line in (REPO_ROOT / filename).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_required_application_source_files_exist() -> None:
    missing = [path for path in REQUIRED_SOURCE_FILES if not (REPO_ROOT / path).is_file()]
    assert not missing, f"Required application source files are missing: {missing}"


def test_runtime_gitignore_directories_are_root_scoped() -> None:
    patterns = _patterns(".gitignore")

    for directory in ROOT_RUNTIME_DIRECTORIES:
        assert f"/{directory}/" in patterns
        assert f"{directory}/" not in patterns


def test_required_application_sources_are_not_gitignored() -> None:
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("Git worktree is unavailable")

    ignored: list[str] = []
    for path in REQUIRED_SOURCE_FILES:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", "--", path],
            cwd=REPO_ROOT,
            check=False,
        )
        if result.returncode == 0:
            ignored.append(path)
        elif result.returncode != 1:
            pytest.fail(f"git check-ignore failed for {path}: {result.returncode}")

    assert not ignored, f"Required application source files are ignored: {ignored}"


def test_docker_build_context_reincludes_required_application_sources() -> None:
    patterns = _patterns(".dockerignore")

    required_exceptions = (
        "!app/data/",
        "!app/data/**",
        "!app/services/uploads/",
        "!app/services/uploads/**",
    )
    for exception in required_exceptions:
        assert exception in patterns

    last_app_data_exception = max(patterns.index(item) for item in required_exceptions[:2])
    assert last_app_data_exception > patterns.index("*.csv")
    assert patterns.index("app/data/pdf_text_cache/") > last_app_data_exception
