"""How the upgrader runs anything outside Python: one injectable runner, so tests never touch the system."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence


@dataclass(frozen=True)
class RunResult:
    code: int
    out: str


Runner = Callable[..., RunResult]


def real_runner(argv: Sequence[str], cwd: Optional[Path] = None, timeout: float = 600.0) -> RunResult:
    try:
        done = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return RunResult(124, f"timed out after {timeout:.0f}s")
    except OSError as exc:
        return RunResult(127, f"{type(exc).__name__}: {exc}")
    return RunResult(done.returncode, (done.stdout or "") + (done.stderr or ""))
