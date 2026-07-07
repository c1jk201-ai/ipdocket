from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULT_THRESHOLDS = {
    "app/services/automation/": 55.0,
    "app/services/deadlines/": 55.0,
    "app/services/matching/": 55.0,
    "app/services/uploads/": 55.0,
    "app/services/workflow/": 50.0,
    "app/services/billing/": 45.0,
}


def _load_thresholds() -> dict[str, float]:
    raw = os.environ.get("CORE_COVERAGE_THRESHOLDS", "").strip()
    if not raw:
        return dict(DEFAULT_THRESHOLDS)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"CORE_COVERAGE_THRESHOLDS must be JSON: {exc}") from exc
    thresholds: dict[str, float] = {}
    for prefix, value in data.items():
        thresholds[str(prefix)] = float(value)
    return thresholds


def _covered_percent(files: dict, prefix: str) -> float | None:
    covered = 0
    statements = 0
    for path, payload in files.items():
        normalized = str(path).replace("\\", "/")
        if not normalized.startswith(prefix):
            continue
        summary = payload.get("summary") or {}
        covered += int(summary.get("covered_lines") or 0)
        statements += int(summary.get("num_statements") or 0)
    if statements <= 0:
        return None
    return round((covered / statements) * 100, 2)


def main() -> int:
    coverage_path = Path(os.environ.get("COVERAGE_JSON", "coverage.json"))
    if not coverage_path.exists():
        print(f"coverage JSON not found: {coverage_path}", file=sys.stderr)
        return 2

    payload = json.loads(coverage_path.read_text(encoding="utf-8"))
    files = payload.get("files") or {}
    thresholds = _load_thresholds()

    failures: list[str] = []
    for prefix, threshold in thresholds.items():
        percent = _covered_percent(files, prefix)
        if percent is None:
            failures.append(f"{prefix}: no measured files")
            continue
        if percent < threshold:
            failures.append(f"{prefix}: {percent:.2f}% < {threshold:.2f}%")

    if failures:
        print("Core coverage thresholds failed:")
        for failure in failures:
            print(f" - {failure}")
        return 1

    print("Core coverage thresholds passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
