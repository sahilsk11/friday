"""Fail CI if any tracked Python file exceeds the line-length cap."""

from __future__ import annotations

import sys
from pathlib import Path

MAX_LINES = 700


def main(roots: list[str]) -> int:
    violations: list[tuple[Path, int]] = []
    for root in roots:
        for path in Path(root).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            line_count = sum(1 for _ in path.open("r", encoding="utf-8"))
            if line_count > MAX_LINES:
                violations.append((path, line_count))

    if not violations:
        return 0

    print(f"Files exceeding {MAX_LINES} lines:", file=sys.stderr)
    for path, count in violations:
        print(f"  {path}: {count} lines", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["friday", "tests"]))
