from __future__ import annotations

import re
from pathlib import Path

PATTERN = re.compile(
    r"^[ \t]*except(?:\s+Exception(?:\s+as\s+\w+)?)?\s*:"
    r"(?:[ \t]*(?:#.*)?\r?\n[ \t]*pass\b|[ \t]*pass\b)",
    re.MULTILINE,
)


def _iter_files() -> list[Path]:
    paths: list[Path] = []
    for base in ("app", "scripts", "migrations", "tests"):
        p = Path(base)
        if p.is_dir():
            paths.extend(p.rglob("*.py"))
        elif p.is_file() and p.suffix == ".py":
            paths.append(p)
    paths.extend(Path(".").glob("*.py"))
    return paths


def _match_line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def test_no_try_except_pass_swallowing() -> None:
    hits: list[str] = []
    for file_path in _iter_files():
        try:
            text = file_path.read_text(encoding="utf-8")
        except Exception:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        match = PATTERN.search(text)
        if match:
            hits.append(f"{file_path}:{_match_line(text, match.start())}")

    assert not hits, "Found try/except/pass swallowing patterns:\n" + "\n".join(hits)
