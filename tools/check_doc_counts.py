"""Fail the build when the docs claim a test count the suite does not have.

The README badge said 559 while the suite was at 570, then 570 while it was at
590. A stale number on the front page is the first thing a reader checks, and
"we forgot" is not a reason to be wrong about it — so the number is verified
instead of trusted.

Run locally:  python tools/check_doc_counts.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Every place the count is written down, with the pattern that finds it.
CLAIMS: tuple[tuple[str, str], ...] = (
    ("README.md", r"tests-(\d+)%20passing"),
    ("README.md", r"pytest tests -q\s+# (\d+) tests"),
    ("README.md", r"tests/\s+(\d+) tests"),
    ("CONTRIBUTING.md", r"pytest tests -q\s+# (\d+) tests"),
)


def claimed_counts() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for name, pattern in CLAIMS:
        text = (REPO / name).read_text(encoding="utf-8")
        found.setdefault(name, []).extend(
            int(match) for match in re.findall(pattern, text)
        )
    return found


def collected_count() -> int:
    """How many tests pytest would run. Collection only: fast and side-effect free."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"(\d+) tests? collected", result.stdout)
    if match is None:
        raise SystemExit(
            "could not read the collected test count from pytest:\n"
            + (result.stdout or result.stderr)[-400:]
        )
    return int(match.group(1))


def main() -> int:
    actual = collected_count()
    problems: list[str] = []
    for name, counts in claimed_counts().items():
        for count in counts:
            if count != actual:
                problems.append(f"{name} claims {count} tests")
    if problems:
        print(f"the suite has {actual} tests, but:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nUpdate the count in the file(s) above, then re-run.",
            file=sys.stderr,
        )
        return 1
    print(f"docs agree with the suite: {actual} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
