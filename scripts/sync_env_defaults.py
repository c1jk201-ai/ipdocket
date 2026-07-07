#!/usr/bin/env python3
"""
Sync missing keys from .env.example into .env.

Usage:
  - Check only (non-zero exit when missing):
      python scripts/sync_env_defaults.py --check
  - Append missing keys using defaults from .env.example:
      python scripts/sync_env_defaults.py --write
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_env_file(path: Path) -> tuple[list[str], dict[str, str]]:
    order: list[str] = []
    values: dict[str, str] = {}

    if not path.exists():
        return order, values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not KEY_RE.match(key):
            continue
        if key not in values:
            order.append(key)
        values[key] = value

    return order, values


def missing_keys(env_path: Path, example_path: Path) -> list[tuple[str, str]]:
    example_order, example_values = parse_env_file(example_path)
    _, env_values = parse_env_file(env_path)

    missing: list[tuple[str, str]] = []
    for key in example_order:
        if key in env_values:
            continue
        missing.append((key, example_values.get(key, "")))
    return missing


def append_missing_keys(env_path: Path, missing: list[tuple[str, str]]) -> None:
    if not missing:
        return
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    lines = [
        "",
        f"# --- Auto-added from .env.example at {ts} ---",
    ]
    lines.extend(f"{key}={value}" for key, value in missing)
    with env_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Sync missing keys from .env.example into .env")
    p.add_argument("--env", default=".env", help="Path to target .env file")
    p.add_argument("--example", default=".env.example", help="Path to .env.example file")
    mode = p.add_mutually_exclusive_group(required=False)
    mode.add_argument(
        "--check",
        action="store_true",
        help="Check mode only. Exit 1 when missing keys are found.",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="Append missing keys to .env using defaults from .env.example.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    env_path = Path(args.env)
    example_path = Path(args.example)

    if not example_path.exists():
        print(f"[env-sync] Missing example file: {example_path}", file=sys.stderr)
        return 2

    missing = missing_keys(env_path, example_path)
    if not missing:
        print("[env-sync] OK: no missing keys.")
        return 0

    print(f"[env-sync] Missing {len(missing)} key(s) in {env_path}:")
    for key, _value in missing:
        print(f"  - {key}")

    if args.write:
        if not env_path.exists():
            env_path.write_text("", encoding="utf-8")
        append_missing_keys(env_path, missing)
        print(f"[env-sync] Appended missing keys to {env_path}.")
        return 0

    # Default behavior and --check: signal missing keys.
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
