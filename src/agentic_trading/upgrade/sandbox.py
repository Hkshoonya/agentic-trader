"""Candidate code runs here: no network, no home directory, only its worktree writable (spec U10, U12).

``bwrap`` builds a fresh namespace for each run. The candidate sees the system's read-only
directories, the project's venv (read-only) and its own worktree; it never sees ``secrets.toml``,
``.env``, broker tokens or ``~/.config``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional, Sequence

from agentic_trading.upgrade.run import RunResult, Runner

PROBE = r"""
import json, pathlib, socket, sys
out = {}
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3)
    out["network"] = True
except OSError:
    out["network"] = False
def readable(p):
    try:
        pathlib.Path(p).read_bytes()[:1]
        return True
    except OSError:
        return False
out["secrets"] = readable(sys.argv[1])
out["home"] = readable(sys.argv[2])
try:
    pathlib.Path(sys.argv[3]).write_text("x")
    out["write_outside"] = True
except OSError:
    out["write_outside"] = False
print(json.dumps(out))
"""
LEAKS = {"network": "the sandbox has network access", "secrets": "the sandbox can read secrets.toml",
         "home": "the sandbox can read the home directory", "write_outside": "the sandbox can write outside its worktree"}


def wrap(argv: Sequence[str], *, worktree: Path, venv: Path) -> list[str]:
    system: list[str] = ["--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc"]
    for top in ("/bin", "/lib", "/lib64", "/sbin"):
        path = Path(top)
        if path.is_symlink():
            system += ["--symlink", str(path.readlink()), top]
        elif path.is_dir():
            system += ["--ro-bind", top, top]
    return ["bwrap", *system,
            "--ro-bind", str(venv), str(venv),
            "--bind", str(worktree), str(worktree),
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--unshare-net", "--unshare-pid", "--die-with-parent", "--clearenv",
            "--setenv", "PATH", f"{venv}/bin:/usr/bin:/bin", "--setenv", "HOME", "/tmp",
            "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--chdir", str(worktree), *argv]


def _python(venv: Path) -> str:
    return str(venv / "bin" / "python")


def probe(runner: Runner, *, worktree: Path, venv: Path, secrets: Path, home_file: Path) -> list[str]:
    outside = worktree.parent / "upgrade-sandbox-probe.txt"
    result = runner(wrap([_python(venv), "-c", PROBE, str(secrets), str(home_file), str(outside)],
                         worktree=worktree, venv=venv), timeout=60.0)
    if result.code != 0:
        return [f"the sandbox would not start: {result.out.strip()[-200:]}"]
    try:
        found = json.loads(result.out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return ["the sandbox probe printed nothing readable"]
    return [text for key, text in LEAKS.items() if found.get(key) is not False]


def count_tests(runner: Runner, *, worktree: Path, venv: Path) -> Optional[int]:
    result = runner(wrap([_python(venv), "-m", "pytest", "tests", "--collect-only", "-q"],
                         worktree=worktree, venv=venv), timeout=600.0)
    match = re.search(r"(\d+) tests? collected", result.out)
    return int(match.group(1)) if result.code == 0 and match else None


def run_suite(runner: Runner, *, worktree: Path, venv: Path) -> RunResult:
    return runner(wrap([_python(venv), "-m", "pytest", "tests", "-q", "-x"], worktree=worktree, venv=venv),
                  timeout=2400.0)


def run_doc_counts(runner: Runner, *, worktree: Path, venv: Path) -> RunResult:
    return runner(wrap([_python(venv), "tools/check_doc_counts.py"], worktree=worktree, venv=venv), timeout=900.0)
