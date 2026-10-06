# Self-Upgrader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Once a day, the system picks one improvement and has Codex write it inside an allow-list, under a sandbox. Trusted code checks it, the tests run sandboxed, and Claude cross-checks it. It ships with a PR record and then watches a 24 h canary that rolls back automatically. One-click Pause, Resume and Roll back are on the dashboard and in the CLI.

**Architecture:**
- **New package:** `src/agentic_trading/upgrade/` (trusted, protected code).
- **Isolation:** all git, systemd, sandbox and engine calls go through one injectable `Runner`, so every test runs with fakes.
- **Two timers:** a daily `upgrade run`, and an `upgrade watch` every 5 minutes.
- **Dashboard:** gains a fenced `POST /api/upgrade` and an Evolution card.
- **Desk:** journals how far its follows drift from the swarm's paper prices.

**Tech Stack:** Python 3.11+ stdlib (`ast`, `subprocess` in trusted code only), `bwrap`, `codex exec`, `claude -p`, `gh`, `systemctl --user`.

**Spec:** `docs/superpowers/specs/2026-10-06-self-upgrade-design.md`

## Global Constraints

- **Commits:** only `git -c user.name="Hkshoonya" -c user.email="154622641+Hkshoonya@users.noreply.github.com" commit`, no AI mention or trailers. The upgrader's own commits use the same identity (U5).
- **Never stage** `data/bars/*`, `data/stream/*`, `config/agentic.toml*` or `config/secrets.toml`. Never read or print secrets.
- **Tests never touch the network, git remotes, systemd or a real engine.** Everything goes through fakes.
- **Allow-list (U1):** `src/agentic_trading/strategies/**`, `src/agentic_trading/swarm/**`, `tests/test_swarm_*.py` except `tests/test_swarm_funding.py` and `tests/test_swarm_desk.py`, `tests/test_strategies_*.py`, `docs/**`, and the test-count lines of `README.md` and `CONTRIBUTING.md`. Live mode removes `strategies/` and `swarm/` (U2).
- **Control modules (U1):**
  - `agentic_trading.upgrade` (the whole package) and `agentic_trading.dashboard`;
  - `agentic_trading.risk`, `.arming`, `.panic`, `.limits`, `.config`, `.journal`, `.jsonio`, `.notify`;
  - `agentic_trading.venues.guard`, `.venues.arming`, `.venues.secrets`.
- **Change shape (U3):** at most 12 files and 400 changed lines. The test count never falls and no test function is deleted. The `ast` scan forbids new `subprocess`, `socket`, `httpx`, `requests`, `urllib`, `http`, `ctypes`, `multiprocessing`, `os.environ`/`os.getenv`/`os.system`/`os.popen`, `eval`/`exec`/`__import__`/`compile`, and `open()` on absolute or `..` paths.
- **Canary (U6):** 24 h; a watchdog every 5 minutes.
- **Branch:** `live` holds the services' code. Upgrade branches are `auto/<YYYY-MM-DD>-<slug>`. Rollback is `git revert` plus a store-snapshot restore, never `reset --hard` (U7).
- **Work location:** in the worktree `/home/doczeus/Projects/agentic-trading-upgrade`. Run tests with `"/home/doczeus/Projects/Agnetic TraDING/.venv/bin/python" -m pytest tests -q`. The test counts in README and CONTRIBUTING are updated in Task 11.

**Plan rulings (made under the user's delegation):**
- **P1. Task ranking:** proposals first, by category (`strategy`, `research`, `reliability`, `data`, `execution`, `risk`), then by confidence descending. Then the backlog, then health. "Earner" work comes first.
- **P2. NOT_APPLICABLE:** an engine answer starting `NOT_APPLICABLE:` marks the task `out_of_scope` permanently, without counting a failure. Most risk and execution proposals live in protected code.
- **P3. Fail-safe control file:** an unreadable `control.json` reads as *paused*.
- **P4. Follow gap:** `(live mid / swarm book price − 1) × 10 000` bp for each symbol a funded read-only member holds after a scoped retarget. The last 60 are kept in `desk.json["follow"]`, and the card shows their median.
- **P5. Seeded backlog:** `backlog.json` starts with improvement themes inside the allow-list (see Task 4), so the evolution has earner work on day one.

## Review Focus

1. **An allowed file that a control module imports, even lazily inside a function.** It must be refused; the `ast` closure walks function bodies too. Task 2 has a test.
2. **Pause pressed while a run is mid-flight.** The run must stop before deploying. Task 8 has a test.
3. **A rollback with no open canary**, the operator's "undo last". It must revert the last shipped commit, never something else. Task 7 has a test.
4. **A corrupt `control.json`.** It reads as paused (P3); it never reads as "go". Task 1 has a test.
5. **The live checkout not on `live`, or dirty outside `data/`.** It refuses; it never ships onto the wrong branch. Task 8 has a test.

## File Structure

| File | Responsibility |
|---|---|
| `src/agentic_trading/upgrade/__init__.py` | Empty |
| `upgrade/run.py` | `RunResult`, `Runner`, `real_runner` |
| `upgrade/settings.py` | `[upgrade]` config |
| `upgrade/control.py` | `control.json`: pause, resume, rollback request, canary, last shipped |
| `upgrade/policy.py` | The allow-list, the control closure, the change shape, the `ast` and secret scans |
| `upgrade/sandbox.py` | The `bwrap` wrapper, the probe, the test count, the suite and doc-count runs |
| `upgrade/tasks.py` | Candidates, picking, the attempt ledger, the seeded backlog |
| `upgrade/engines.py` | Codex writes, Claude reviews |
| `upgrade/ship.py` | Worktree, diff, commit, PR, deploy, snapshot, rollback |
| `upgrade/watchdog.py` | Canary checks, rollback execution |
| `upgrade/cycle.py` | One daily run, plus `upgrade.json` for the dashboard |
| `upgrade/cli.py` | `agentic-trading upgrade run\|watch\|pause\|resume\|rollback\|status` |
| `notify.py` (modify) | Alert on `upgrade_rolled_back` |
| `dashboard.py` + `dashboard_upgrade.py` + the html/css/js modules | `GET`/`POST /api/upgrade`, the Evolution card |
| `desk/desk.py` (modify), `dashboard_swarm.py` | The follow gap |
| `deploy/agentic-trading-upgrade{,.service,.timer}`, `deploy/agentic-trading-upgrade-watch{.service,.timer}` | Timers |

---

### Task 1: Runner, settings and control

**Files:**
- Create: `src/agentic_trading/upgrade/__init__.py`, `upgrade/run.py`, `upgrade/settings.py`, `upgrade/control.py`
- Test: `tests/test_upgrade_control.py`

**Interfaces:**
- **Produces, runner:** `RunResult(code: int, out: str)`, `Runner = Callable[..., RunResult]` (called as `runner(argv, cwd=None, timeout=600.0)`), `real_runner`.
- **Produces, settings:** `UpgradeConfig(enabled=False, canary_hours=24, max_files=12, max_lines=400)`, `load_upgrade_config(path)`.
- **Produces, control:** `Control(paused, reason, rollback_requested, canary: dict, last_shipped: dict)`, with `load_control(state_dir)`, `save_control(state_dir, c)`, `pause(state_dir, reason)`, `resume(state_dir)` and `request_rollback(state_dir)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_upgrade_control.py
"""The upgrader's switches: off by default, pause always wins, a broken file reads as paused."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentic_trading.upgrade.control import load_control, pause, request_rollback, resume, save_control
from agentic_trading.upgrade.run import RunResult, real_runner
from agentic_trading.upgrade.settings import load_upgrade_config


class SettingsTests(unittest.TestCase):
    def _load(self, text: str):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "agentic.toml"
            path.write_text(text)
            return load_upgrade_config(path)

    def test_off_by_default_and_strict(self) -> None:
        config = self._load("")
        self.assertEqual((config.enabled, config.canary_hours, config.max_files, config.max_lines),
                         (False, 24, 12, 400))
        for text, needle in {'[upgrade]\nenabeld = true\n': "unknown keys",
                             '[upgrade]\nenabled = "true"\n': "upgrade.enabled",
                             '[upgrade]\nmax_lines = 5000\n': "upgrade.max_lines"}.items():
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, needle):
                self._load(text)


class ControlTests(unittest.TestCase):
    def test_pause_resume_and_rollback_requests(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            self.assertFalse(load_control(state).paused)
            pause(state, "operator pressed pause")
            self.assertEqual((load_control(state).paused, load_control(state).reason), (True, "operator pressed pause"))
            request_rollback(state)
            self.assertTrue(load_control(state).rollback_requested)
            resume(state)
            control = load_control(state)
            self.assertEqual((control.paused, control.reason, control.rollback_requested), (False, "", True))

    def test_an_unreadable_control_file_reads_as_paused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            (state / "upgrade").mkdir()
            (state / "upgrade" / "control.json").write_text("{nope")
            control = load_control(state)
        self.assertTrue(control.paused)
        self.assertIn("unreadable", control.reason)

    def test_canary_and_last_shipped_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            control = load_control(state)
            control.canary = {"commit": "abc", "until": "2026-10-07T02:00:00+00:00"}
            control.last_shipped = {"commit": "abc", "title": "t"}
            save_control(state, control)
            again = load_control(state)
        self.assertEqual((again.canary["commit"], again.last_shipped["title"]), ("abc", "t"))


class RunnerTests(unittest.TestCase):
    def test_the_real_runner_reports_exit_codes_and_timeouts(self) -> None:
        ok = real_runner(["python3", "-c", "print('hi')"])
        self.assertEqual((ok.code, ok.out.strip()), (0, "hi"))
        slow = real_runner(["python3", "-c", "import time; time.sleep(5)"], timeout=0.5)
        self.assertEqual(slow.code, 124)
        missing = real_runner(["no-such-binary-xyz"])
        self.assertEqual(missing.code, 127)
        self.assertIsInstance(ok, RunResult)
```

- [ ] **Step 2: Run them and confirm they fail.** Run `... -m pytest tests/test_upgrade_control.py -q`. Expected: FAIL with `ModuleNotFoundError: agentic_trading.upgrade`.

- [ ] **Step 3: Implement**

`upgrade/__init__.py` stays empty.

`upgrade/run.py`:
```python
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
```

`upgrade/settings.py`:
```python
"""The ``[upgrade]`` table: off unless ``enabled = true``; misspelled keys are errors."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class UpgradeConfig:
    enabled: bool = False
    canary_hours: int = 24
    max_files: int = 12
    max_lines: int = 400


LIMITS = {"canary_hours": (1, 168), "max_files": (1, 12), "max_lines": (1, 400)}  # the walls only tighten


def load_upgrade_config(path: Path | str) -> UpgradeConfig:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("upgrade") or {}
    if not isinstance(raw, dict):
        raise ValueError("[upgrade] must be a table")
    unknown = sorted(set(raw) - {f.name for f in fields(UpgradeConfig)})
    if unknown:
        raise ValueError(f"[upgrade] has unknown keys {unknown}")
    values: dict[str, Any] = {}
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise ValueError("upgrade.enabled must be true or false, without quotes")
        values["enabled"] = raw["enabled"]
    for key, (low, high) in LIMITS.items():
        if key in raw:
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"upgrade.{key} must be a whole number from {low} to {high}")
            values[key] = value
    return UpgradeConfig(**values)
```

`upgrade/control.py`:
```python
"""``state/upgrade/control.json``: the one-click switches and the open canary.

Pause always wins. A file that can't be read reads as paused: the upgrader must never take a broken
switch for permission.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentic_trading import jsonio


@dataclass
class Control:
    paused: bool = False
    reason: str = ""
    rollback_requested: bool = False
    canary: dict[str, Any] = field(default_factory=dict)
    last_shipped: dict[str, Any] = field(default_factory=dict)


def _path(state_dir: Path | str) -> Path:
    return Path(state_dir) / "upgrade" / "control.json"


def load_control(state_dir: Path | str) -> Control:
    path = _path(state_dir)
    if not path.is_file():
        return Control()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Control(bool(raw.get("paused")), str(raw.get("reason") or ""),
                       bool(raw.get("rollback_requested")), dict(raw.get("canary") or {}),
                       dict(raw.get("last_shipped") or {}))
    except (OSError, ValueError, TypeError, AttributeError):
        return Control(paused=True, reason="control.json is unreadable: paused until it is fixed")


def save_control(state_dir: Path | str, control: Control) -> None:
    path = _path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_text(path, jsonio.dumps(asdict(control), indent=2) + "\n")


def pause(state_dir: Path | str, reason: str) -> Control:
    control = load_control(state_dir)
    control.paused, control.reason = True, reason
    save_control(state_dir, control)
    return control


def resume(state_dir: Path | str) -> Control:
    control = load_control(state_dir)
    control.paused, control.reason = False, ""
    save_control(state_dir, control)
    return control


def request_rollback(state_dir: Path | str) -> Control:
    control = load_control(state_dir)
    control.rollback_requested = True
    save_control(state_dir, control)
    return control
```

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat(upgrade): runner, [upgrade] settings, and control switches (pause wins; a broken file reads as paused)`.

---
### Task 2: The walls (policy)

**Files:**
- Create: `src/agentic_trading/upgrade/policy.py`
- Test: `tests/test_upgrade_policy.py`

**Interfaces:**
- **Produces, the change model:** `FileChange(path, status, old_path="", added: tuple[str, ...]=(), removed: tuple[str, ...]=())` and `parse_diff(name_status: str, unified: str) -> list[FileChange]`.
- **Produces, path rules:** `allowed_path(path, *, live) -> bool`, `module_of(path) -> Optional[str]`, `control_closure(src_root: Path) -> set[str]`.
- **Produces, scans:** `scan(text) -> Counter[str]` and `test_names(text) -> set[str]`.
- **Produces, the gate:** `check(changes, *, live, closure, before: dict[str, str], after: dict[str, str], tests_before, tests_after, max_files, max_lines) -> list[str]`. It returns the reasons for refusal; an empty list means the change may proceed.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_upgrade_policy.py
"""The upgrader's walls: what it may touch, what it may write, how big a change may be."""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from agentic_trading.upgrade.policy import (
    FileChange, allowed_path, check, control_closure, module_of, parse_diff, scan,
)

OK = dict(live=False, closure=set(), tests_before=10, tests_after=11, max_files=12, max_lines=400)
STRAT = "src/agentic_trading/strategies/trend_crypto.py"


def _check(changes, before=None, after=None, **over):
    return check(changes, before=before or {}, after=after or {}, **{**OK, **over})


class PathTests(unittest.TestCase):
    def test_the_allow_list_and_live_mode(self) -> None:
        for path in (STRAT, "src/agentic_trading/swarm/blend.py", "tests/test_swarm_blend.py",
                     "tests/test_strategies_x.py", "docs/notes.md"):
            self.assertTrue(allowed_path(path, live=False), path)
        for path in ("src/agentic_trading/risk.py", "src/agentic_trading/desk/desk.py", "tests/test_swarm_funding.py",
                     "tests/test_swarm_desk.py", "tests/conftest.py", "src/agentic_trading/upgrade/policy.py",
                     "deploy/x.service", "pyproject.toml", "config/agentic.toml", "README.md"):
            self.assertFalse(allowed_path(path, live=False), path)
        self.assertFalse(allowed_path(STRAT, live=True))
        self.assertTrue(allowed_path("docs/notes.md", live=True))

    def test_module_names(self) -> None:
        self.assertEqual(module_of("src/agentic_trading/swarm/blend.py"), "agentic_trading.swarm.blend")
        self.assertEqual(module_of("src/agentic_trading/swarm/__init__.py"), "agentic_trading.swarm")
        self.assertIsNone(module_of("docs/x.md"))

    def test_the_control_closure_follows_imports_even_inside_functions(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "src" / "agentic_trading"
            (root / "swarm").mkdir(parents=True)
            (root / "__init__.py").write_text("")
            (root / "swarm" / "__init__.py").write_text("")
            (root / "dashboard.py").write_text("def view():\n    from agentic_trading.helper import x\n")
            (root / "helper.py").write_text("from agentic_trading.swarm import deep\n")
            (root / "swarm" / "deep.py").write_text("X = 1\n")
            (root / "swarm" / "free.py").write_text("Y = 2\n")
            closure = control_closure(Path(name) / "src")
        self.assertIn("agentic_trading.helper", closure)
        self.assertIn("agentic_trading.swarm.deep", closure)
        self.assertNotIn("agentic_trading.swarm.free", closure)


class DiffTests(unittest.TestCase):
    def test_parse_name_status_and_lines(self) -> None:
        name_status = "M\tsrc/agentic_trading/swarm/blend.py\nA\tdocs/n.md\nR100\told.py\tnew.py\nD\tgone.py\n"
        unified = textwrap.dedent("""\
            diff --git a/src/agentic_trading/swarm/blend.py b/src/agentic_trading/swarm/blend.py
            --- a/src/agentic_trading/swarm/blend.py
            +++ b/src/agentic_trading/swarm/blend.py
            @@ -1 +1 @@
            -X = 1
            +X = 2
            diff --git a/docs/n.md b/docs/n.md
            +++ b/docs/n.md
            +hello
            """)
        changes = {c.path: c for c in parse_diff(name_status, unified)}
        self.assertEqual(changes["src/agentic_trading/swarm/blend.py"].added, ("X = 2",))
        self.assertEqual(changes["src/agentic_trading/swarm/blend.py"].removed, ("X = 1",))
        self.assertEqual((changes["new.py"].status, changes["new.py"].old_path), ("R", "old.py"))
        self.assertEqual(changes["gone.py"].status, "D")


class CheckTests(unittest.TestCase):
    def test_a_small_allowed_change_passes(self) -> None:
        change = FileChange(STRAT, "M", added=("x = 2",), removed=("x = 1",))
        self.assertEqual(_check([change], {STRAT: "x = 1\n"}, {STRAT: "x = 2\n"}), [])

    def test_each_wall(self) -> None:
        cases = {
            "outside": [FileChange("src/agentic_trading/risk.py", "M", added=("a",))],
            "deletes": [FileChange(STRAT, "D")],
            "renames": [FileChange("src/agentic_trading/risk2.py", "R", old_path=STRAT)],
        }
        for needle, changes in cases.items():
            with self.subTest(needle=needle):
                self.assertTrue(any(needle in r for r in _check(changes)), _check(changes))
        imported = [FileChange("src/agentic_trading/swarm/deep.py", "M", added=("a",))]
        self.assertTrue(any("control module" in r for r in
                            _check(imported, closure={"agentic_trading.swarm.deep"})))
        self.assertTrue(any("test count" in r for r in _check([], tests_after=9)))
        big = [FileChange(f"docs/{i}.md", "A", added=("x",)) for i in range(13)]
        self.assertTrue(any("files" in r for r in _check(big)))
        long = [FileChange("docs/a.md", "A", added=tuple("x" for _ in range(401)))]
        self.assertTrue(any("lines" in r for r in _check(long)))

    def test_forbidden_code_is_refused_only_when_new(self) -> None:
        before = {STRAT: "import os\n"}
        for code, needle in (("import subprocess\n", "subprocess"), ("import socket\n", "socket"),
                             ("x = os.environ['K']\n", "os.environ"), ("eval('1')\n", "eval"),
                             ("open('/etc/passwd')\n", "open"), ("import httpx\n", "httpx")):
            with self.subTest(code=code):
                after = {STRAT: "import os\n" + code}
                reasons = _check([FileChange(STRAT, "M", added=(code.strip(),))], before, after)
                self.assertTrue(any(needle in r for r in reasons), reasons)
        same = {STRAT: "import subprocess\n"}
        self.assertEqual(_check([FileChange(STRAT, "M", added=("y = 1",))], same,
                                {STRAT: "import subprocess\ny = 1\n"}), [])

    def test_a_deleted_test_and_a_secret_are_refused(self) -> None:
        path = "tests/test_swarm_blend.py"
        before = {path: "def test_a():\n    pass\ndef test_b():\n    pass\n"}
        after = {path: "def test_a():\n    pass\n"}
        self.assertTrue(any("deletes test" in r for r in _check([FileChange(path, "M", removed=("x",))],
                                                                before, after)))
        leak = [FileChange("docs/a.md", "A", added=('api_key = "sk-abcdefghijklmnopqrstuvwxyz123456"',))]
        self.assertTrue(any("secret" in r for r in _check(leak)))

    def test_readme_may_change_only_its_test_counts(self) -> None:
        fine = [FileChange("README.md", "M", added=("[![tests](https://img.shields.io/badge/tests-1300%20passing-35d07f)](#verify)",),
                           removed=("[![tests](https://img.shields.io/badge/tests-1279%20passing-35d07f)](#verify)",))]
        self.assertEqual(_check(fine), [])
        prose = [FileChange("README.md", "M", added=("The system is now allowed to trade live.",))]
        self.assertTrue(any("README.md" in r for r in _check(prose)))

    def test_forbidden_constructs_are_counted(self) -> None:
        self.assertEqual(scan("import subprocess\nimport subprocess as s\n")["subprocess"], 2)
        self.assertEqual(sum(scan("x = 1\n").values()), 0)
```

- [ ] **Step 2: Run them and confirm they fail.** Expected: `ModuleNotFoundError: agentic_trading.upgrade.policy`.

- [ ] **Step 3: Implement `upgrade/policy.py`**

```python
"""The upgrader's walls, enforced by trusted code on every candidate (spec U1–U3).

The editable surface is an allow-list, and even an allowed file is refused when any control
module imports it, however indirectly: whatever a control imports runs inside the control.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

ALLOWED_DIRS = ("src/agentic_trading/strategies/", "src/agentic_trading/swarm/", "docs/")
LIVE_REMOVED = ("src/agentic_trading/strategies/", "src/agentic_trading/swarm/")
ALLOWED_TEST_PREFIXES = ("tests/test_swarm_", "tests/test_strategies_")
PROTECTED_TESTS = ("tests/test_swarm_funding.py", "tests/test_swarm_desk.py")
COUNT_FILES = ("README.md", "CONTRIBUTING.md")
COUNT_LINE = re.compile(r"tests-\d+%20passing|#\s*\d+ tests|tests/\s+\d+ tests")
CONTROL_MODULES = (
    "agentic_trading.upgrade", "agentic_trading.dashboard", "agentic_trading.risk", "agentic_trading.arming",
    "agentic_trading.panic", "agentic_trading.limits", "agentic_trading.config", "agentic_trading.journal",
    "agentic_trading.jsonio", "agentic_trading.notify", "agentic_trading.venues.guard",
    "agentic_trading.venues.arming", "agentic_trading.venues.secrets",
)
FORBIDDEN_MODULES = ("subprocess", "socket", "httpx", "requests", "urllib", "http", "ctypes", "multiprocessing")
FORBIDDEN_CALLS = ("eval", "exec", "__import__", "compile")
FORBIDDEN_OS = ("environ", "getenv", "system", "popen", "putenv", "unsetenv", "execv", "execve", "spawnv", "fork")
SECRET = re.compile(
    r"sk-[A-Za-z0-9_\-]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[abpr]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY|(?i:(api[_-]?key|secret|token|passw(or)?d)\s*[:=]\s*['\"][^'\"\s]{16,}['\"])")


@dataclass(frozen=True)
class FileChange:
    path: str
    status: str  # A, M, D or R
    old_path: str = ""
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


def parse_diff(name_status: str, unified: str) -> list[FileChange]:
    lines: dict[str, tuple[list[str], list[str]]] = {}
    current = ""
    for line in unified.splitlines():
        if line.startswith("diff --git "):
            current = line.split(" b/", 1)[-1]
            lines.setdefault(current, ([], []))
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+") and current:
            lines[current][0].append(line[1:])
        elif line.startswith("-") and current:
            lines[current][1].append(line[1:])
    out = []
    for row in name_status.splitlines():
        parts = row.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][:1]
        path, old = (parts[2], parts[1]) if status == "R" and len(parts) > 2 else (parts[1], "")
        added, removed = lines.get(path, ([], []))
        out.append(FileChange(path, status, old, tuple(added), tuple(removed)))
    return out


def allowed_path(path: str, *, live: bool) -> bool:
    if path in PROTECTED_TESTS:
        return False
    if path.startswith(ALLOWED_TEST_PREFIXES) and path.endswith(".py"):
        return not live  # live mode removes swarm/ and strategies/, and so their tests
    if live and path.startswith(LIVE_REMOVED):
        return False
    return path.startswith(ALLOWED_DIRS)


def module_of(path: str) -> Optional[str]:
    if not (path.startswith("src/") and path.endswith(".py")):
        return None
    dotted = path[len("src/"):-len(".py")].replace("/", ".")
    return dotted[: -len(".__init__")] if dotted.endswith(".__init__") else dotted


def _file_for(src_root: Path, module: str) -> Optional[Path]:
    base = src_root.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imports(path: Path, module: str) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return set()
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1] + ([base] if base else []))
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return {name for name in found if name.startswith("agentic_trading")}


def control_closure(src_root: Path) -> set[str]:
    """Every module a control module imports, directly or not (function-level imports included)."""
    todo = list(CONTROL_MODULES)
    for module in CONTROL_MODULES:  # a control package's submodules are controls too
        folder = src_root.joinpath(*module.split("."))
        if folder.is_dir():
            todo += [f"{module}.{p.stem}" for p in folder.glob("*.py") if p.stem != "__init__"]
    seen: set[str] = set()
    while todo:
        module = todo.pop()
        if module in seen:
            continue
        seen.add(module)
        path = _file_for(src_root, module)
        if path is not None:
            todo.extend(_imports(path, module) - seen)
    return seen


def scan(text: str) -> Counter:
    found: Counter = Counter()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        found["unparseable code"] += 1
        return found
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module]
        for name in names:
            root = name.split(".")[0]
            if root in FORBIDDEN_MODULES:
                found[root] += 1
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            found[node.func.id] += 1
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os" \
                and node.attr in FORBIDDEN_OS:
            found[f"os.{node.attr}"] += 1
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) \
                    and (first.value.startswith("/") or ".." in first.value):
                found["open outside the repo"] += 1
    return found


def test_names(text: str) -> set[str]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names.update(f"{node.name}.{f.name}" for f in node.body
                         if isinstance(f, ast.FunctionDef) and f.name.startswith("test"))
        elif isinstance(node, ast.FunctionDef) and node.name.startswith("test") and node.col_offset == 0:
            names.add(node.name)
    return names


def check(changes: Iterable[FileChange], *, live: bool, closure: set[str], before: dict[str, str],
          after: dict[str, str], tests_before: int, tests_after: int, max_files: int, max_lines: int) -> list[str]:
    changes = list(changes)
    reasons: list[str] = []
    for change in changes:
        if change.status == "D":
            reasons.append(f"deletes {change.path}: the upgrader may not delete files")
            continue
        if change.status == "R" and not (allowed_path(change.old_path, live=live)
                                         and allowed_path(change.path, live=live)):
            reasons.append(f"renames {change.old_path} to {change.path}: both must be inside what it may change")
            continue
        if change.path in COUNT_FILES:
            if not all(COUNT_LINE.search(line) for line in change.added + change.removed):
                reasons.append(f"changes {change.path} beyond its test counts")
        elif not allowed_path(change.path, live=live):
            reasons.append(f"touches {change.path}, which is outside what the upgrader may change")
        module = module_of(change.path)
        if module and module in closure:
            reasons.append(f"{change.path} is imported by a control module, so it is protected")
        if change.path.endswith(".py"):
            new = scan(after.get(change.path, "")) - scan(before.get(change.path, ""))
            reasons.extend(f"adds {kind} to {change.path}" for kind in sorted(new))
            if change.path.startswith("tests/"):
                gone = test_names(before.get(change.path, "")) - test_names(after.get(change.path, ""))
                reasons.extend(f"deletes test {name} in {change.path}" for name in sorted(gone))
        if any(SECRET.search(line) for line in change.added):
            reasons.append(f"adds something shaped like a secret to {change.path}")
    if len(changes) > max_files:
        reasons.append(f"changes {len(changes)} files (limit {max_files})")
    size = sum(len(c.added) + len(c.removed) for c in changes)
    if size > max_lines:
        reasons.append(f"changes {size} lines (limit {max_lines})")
    if tests_after < tests_before:
        reasons.append(f"the test count fell from {tests_before} to {tests_after}")
    return reasons
```

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat(upgrade): the walls — allow-list, control-import closure, change shape, ast and secret scans`.

---
### Task 3: The sandbox

**Files:**
- Create: `src/agentic_trading/upgrade/sandbox.py`
- Test: `tests/test_upgrade_sandbox.py`

**Interfaces:**
- Consumes: `RunResult`, `Runner` (Task 1).
- **Produces:**
  - `wrap(argv, *, worktree: Path, venv: Path) -> list[str]`
  - `probe(runner, *, worktree, venv, secrets: Path, home_file: Path) -> list[str]` (the problems found)
  - `count_tests(runner, *, worktree, venv) -> Optional[int]`
  - `run_suite(runner, *, worktree, venv) -> RunResult`
  - `run_doc_counts(runner, *, worktree, venv) -> RunResult`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_upgrade_sandbox.py
"""Candidate code runs without network, without the home directory, writing only its worktree."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from agentic_trading.upgrade.run import RunResult
from agentic_trading.upgrade.sandbox import count_tests, probe, wrap

WT, VENV = Path("/work/auto"), Path("/home/u/repo/.venv")


class _Fake:
    def __init__(self, result: RunResult) -> None:
        self.result, self.calls = result, []

    def __call__(self, argv, cwd=None, timeout=600.0) -> RunResult:
        self.calls.append(list(argv))
        return self.result


class SandboxTests(unittest.TestCase):
    def test_the_wrapper_cuts_the_network_and_hides_home(self) -> None:
        argv = wrap(["python", "-V"], worktree=WT, venv=VENV)
        self.assertEqual(argv[0], "bwrap")
        self.assertIn("--unshare-net", argv)
        self.assertIn("--clearenv", argv)
        writable = [argv[i + 2] for i, a in enumerate(argv) if a == "--bind"]
        self.assertEqual(writable, [str(WT)])  # the only writable place
        self.assertNotIn("/home", argv)
        self.assertNotIn("/home/u", argv)
        self.assertEqual(argv[-2:], ["python", "-V"])

    def test_the_probe_reports_each_leak(self) -> None:
        clean = _Fake(RunResult(0, json.dumps({"network": False, "secrets": False, "home": False,
                                                "write_outside": False}) + "\n"))
        self.assertEqual(probe(clean, worktree=WT, venv=VENV, secrets=Path("/s"), home_file=Path("/h")), [])
        leaky = _Fake(RunResult(0, json.dumps({"network": True, "secrets": True, "home": False,
                                                "write_outside": True})))
        problems = probe(leaky, worktree=WT, venv=VENV, secrets=Path("/s"), home_file=Path("/h"))
        self.assertEqual(len(problems), 3)
        broken = _Fake(RunResult(1, "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted"))
        [problem] = probe(broken, worktree=WT, venv=VENV, secrets=Path("/s"), home_file=Path("/h"))
        self.assertIn("would not start", problem)
        self.assertIn("RTM_NEWADDR", problem)

    def test_counting_tests(self) -> None:
        self.assertEqual(count_tests(_Fake(RunResult(0, "...\n1279 tests collected in 2.31s\n")), worktree=WT,
                                     venv=VENV), 1279)
        self.assertIsNone(count_tests(_Fake(RunResult(2, "error")), worktree=WT, venv=VENV))
```

- [ ] **Step 2: Run them and confirm they fail.** Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `upgrade/sandbox.py`**

```python
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
```

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat(upgrade): the bwrap sandbox for candidate code, and its leak probe`.

---

### Task 4: What to work on

**Files:**
- Create: `src/agentic_trading/upgrade/tasks.py`
- Test: `tests/test_upgrade_tasks.py`

**Interfaces:**
- **Produces:**
  - `Task(key, title, detail, source)`
  - `SEED_BACKLOG: list[dict]`
  - `candidates(state_dir) -> list[Task]`
  - `pick(state_dir, now) -> Optional[Task]`
  - `record(state_dir, task, outcome: str, now, note="")`, where outcome is one of `shipped`, `out_of_scope`, `failed` or `rolled_back`
  - `note_health(state_dir, problem: str)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_upgrade_tasks.py
"""Earner work first; a task that fails twice rests 30 days; out-of-scope work is never retried."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_trading.upgrade.tasks import SEED_BACKLOG, candidates, note_health, pick, record

NOW = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)


def _proposals(state: Path, rows: list[dict]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "proposals.json").write_text(json.dumps({"proposals": rows}))


class TaskTests(unittest.TestCase):
    def test_strategy_proposals_come_first_then_the_backlog_then_health(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            _proposals(state, [
                {"title": "Tighten risk caps", "category": "risk", "confidence": 0.95, "status": "proposed",
                 "change": "c1"},
                {"title": "Better momentum", "category": "strategy", "confidence": 0.6, "status": "proposed",
                 "change": "c2"},
                {"title": "Done already", "category": "strategy", "confidence": 0.9, "status": "applied",
                 "change": "c3"},
            ])
            note_health(state, "agentic-trading is not active")
            found = candidates(state)
        titles = [t.title for t in found]
        self.assertEqual(titles[0], "Better momentum")
        self.assertEqual(titles[1], "Tighten risk caps")
        self.assertNotIn("Done already", titles)
        self.assertEqual(found[2].source, "backlog")
        self.assertEqual(found[-1].source, "health")
        self.assertEqual(len([t for t in found if t.source == "backlog"]), len(SEED_BACKLOG))

    def test_failures_rest_and_out_of_scope_is_final(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            _proposals(state, [{"title": "A", "category": "strategy", "confidence": 0.9, "status": "proposed",
                                "change": "a"}])
            first = pick(state, NOW)
            record(state, first, "failed", NOW)
            self.assertEqual(pick(state, NOW).key, first.key)  # one failure: still eligible
            record(state, first, "failed", NOW)
            self.assertNotEqual(pick(state, NOW).key, first.key)  # two: rests
            self.assertEqual(pick(state, NOW + timedelta(days=31)).key, first.key)  # back after 30 days
            record(state, first, "out_of_scope", NOW, "lives in runtime.py")
            self.assertNotEqual(pick(state, NOW + timedelta(days=90)).key, first.key)
            ledger = json.loads((state / "upgrade" / "attempts.json").read_text())
        self.assertEqual(ledger[first.key]["outcome"], "out_of_scope")
```

- [ ] **Step 2: Run them and confirm they fail.** Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `upgrade/tasks.py`**

```python
"""What the upgrader works on, in order (spec U9, plan P1/P2/P5)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from agentic_trading import jsonio

CATEGORY_ORDER = ("strategy", "research", "reliability", "data", "execution", "risk")
REST = timedelta(days=30)
FINAL = ("shipped", "out_of_scope")
SEED_BACKLOG: list[dict[str, str]] = [
    {"key": "seed-swarm-shares", "title": "Weight the swarm's contributors by forward evidence, not volatility alone",
     "detail": "In swarm/blend.py, shares are inverse-volatility. Make each contributor's share also grow with the "
               "t-statistic of its trailing forward excess (shrunk toward zero for short records), so proven "
               "agents earn more of the book. Test the ordering and that shares still sum to 1."},
    {"key": "seed-swarm-sizing", "title": "Add a volatility-targeted sizing choice to swarm recipes",
     "detail": "Extend swarm/recipe.py SIZING with a sizing mode that targets a fixed annualised volatility per "
               "position, keeping old recipes' ids unchanged. Test validation, ids and agent_weights."},
    {"key": "seed-swarm-rejections", "title": "Let refused recipes be screened again after 90 days",
     "detail": "swarm/step.py remembers refused recipe ids forever. History moves; let a refusal expire after "
               "90 days (store the refusal date in the ledger) so a recipe can be retried on new data."},
    {"key": "seed-swarm-lock", "title": "Write the swarm step lock atomically",
     "detail": "swarm/store.py creates the lock file, then writes its time; an empty file reads as stale. "
               "Write the timestamp so a just-created lock is never taken over. Test the race."},
    {"key": "seed-swarm-first-day", "title": "Start a newborn's forward record on its first full day",
     "detail": "swarm/life.py: a newborn's first forward day shows minus that day's benchmark move because it "
               "enters at the close. Exclude the entry day from excess. Test with a known first day."},
]


@dataclass(frozen=True)
class Task:
    key: str
    title: str
    detail: str
    source: str  # proposal, backlog or health


def _upgrade_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / "upgrade"


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_text(path, jsonio.dumps(payload, indent=2) + "\n")


def _key(title: str) -> str:
    return "proposal-" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:10]


def candidates(state_dir: Path | str) -> list[Task]:
    raw = _read(Path(state_dir) / "proposals.json", {})
    rows = [p for p in (raw.get("proposals") if isinstance(raw, dict) else []) or []
            if isinstance(p, dict) and p.get("status") == "proposed" and p.get("title")]

    def rank(p: dict) -> tuple:
        category = str(p.get("category") or "")
        order = CATEGORY_ORDER.index(category) if category in CATEGORY_ORDER else len(CATEGORY_ORDER)
        return order, -float(p.get("confidence") or 0)

    out = [Task(_key(str(p["title"])), str(p["title"]),
                f"{p.get('change') or ''}\nExpected effect: {p.get('expected_effect') or ''}\n"
                f"Falsified if: {p.get('falsified_if') or ''}", "proposal") for p in sorted(rows, key=rank)]
    backlog_path = _upgrade_dir(state_dir) / "backlog.json"
    if not backlog_path.is_file():
        _write(backlog_path, SEED_BACKLOG)
    out += [Task(str(b["key"]), str(b["title"]), str(b.get("detail") or ""), "backlog")
            for b in _read(backlog_path, []) if isinstance(b, dict) and b.get("key")]
    out += [Task(str(h["key"]), str(h["title"]), str(h.get("detail") or ""), "health")
            for h in _read(_upgrade_dir(state_dir) / "health.json", []) if isinstance(h, dict) and h.get("key")]
    return out


def pick(state_dir: Path | str, now: datetime) -> Optional[Task]:
    ledger = _read(_upgrade_dir(state_dir) / "attempts.json", {})
    for task in candidates(state_dir):
        entry = ledger.get(task.key) or {}
        if entry.get("outcome") in FINAL:
            continue
        recent = [f for f in entry.get("failures", []) if now - datetime.fromisoformat(f) < REST]
        if len(recent) >= 2:
            continue
        return task
    return None


def record(state_dir: Path | str, task: Task, outcome: str, now: datetime, note: str = "") -> None:
    path = _upgrade_dir(state_dir) / "attempts.json"
    ledger = _read(path, {})
    entry = ledger.setdefault(task.key, {"title": task.title, "failures": []})
    entry.update(outcome=outcome, note=note[:300], last=now.isoformat())
    if outcome in ("failed", "rolled_back"):
        entry["failures"].append(now.isoformat())
    _write(path, ledger)


def note_health(state_dir: Path | str, problem: str) -> None:
    path = _upgrade_dir(state_dir) / "health.json"
    rows = _read(path, [])
    key = "health-" + hashlib.sha1(problem.encode("utf-8")).hexdigest()[:10]
    if not any(r.get("key") == key for r in rows if isinstance(r, dict)):
        rows.append({"key": key, "title": f"Investigate: {problem}", "detail": problem})
        _write(path, rows[-50:])
```

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat(upgrade): what to work on — earner proposals first, a seeded backlog, rests and final outcomes`.

---
### Task 5: The engines

**Files:**
- Create: `src/agentic_trading/upgrade/engines.py`
- Test: `tests/test_upgrade_engines.py`

**Interfaces:**
- Consumes: `Task` (Task 4), `Runner` (Task 1).
- **Produces:**
  - `EngineResult(status, summary)`, where status is `changed`, `not_applicable` or `failed`
  - `write(task, *, worktree, scratch, runner, timeout=2700.0) -> EngineResult`
  - `Verdict(approved: bool, reason: str)`
  - `review(task, *, worktree, base, runner, timeout=1200.0) -> Verdict`
  - `WRITE_RULES` and `REVIEW_RULES`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_upgrade_engines.py
"""Codex writes inside its sandbox; Claude must say APPROVE; anything unclear is a no."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentic_trading.upgrade.engines import review, write
from agentic_trading.upgrade.run import RunResult
from agentic_trading.upgrade.tasks import Task

TASK = Task("k", "Improve shares", "details", "backlog")


class _Fake:
    def __init__(self, code: int, out: str, last: str | None = None) -> None:
        self.code, self.out, self.last, self.calls = code, out, last, []

    def __call__(self, argv, cwd=None, timeout=600.0) -> RunResult:
        self.calls.append(list(argv))
        if self.last is not None and "-o" in argv:
            Path(argv[argv.index("-o") + 1]).write_text(self.last)
        return RunResult(self.code, self.out)


class EngineTests(unittest.TestCase):
    def test_codex_runs_sandboxed_in_the_worktree_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            wt, scratch = Path(name) / "wt", Path(name) / "s"
            wt.mkdir()
            scratch.mkdir()
            fake = _Fake(0, "", "work...\nDONE: shares now grow with evidence")
            result = write(TASK, worktree=wt, scratch=scratch, runner=fake)
            argv = fake.calls[0]
            self.assertEqual(argv[:2], ["codex", "exec"])
            self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
            self.assertEqual(argv[argv.index("-C") + 1], str(wt))
            self.assertIn("src/agentic_trading/swarm/", argv[-1])  # the allow-list rides in the prompt
            self.assertEqual((result.status, result.summary), ("changed", "shares now grow with evidence"))
            na = write(TASK, worktree=wt, scratch=scratch, runner=_Fake(0, "", "NOT_APPLICABLE: lives in runtime.py"))
            self.assertEqual((na.status, na.summary), ("not_applicable", "lives in runtime.py"))
            bad = write(TASK, worktree=wt, scratch=scratch, runner=_Fake(1, "bwrap: failed"))
            self.assertEqual(bad.status, "failed")

    def test_claude_must_approve(self) -> None:
        wt = Path("/tmp")
        yes = _Fake(0, "looks right\nVERDICT: APPROVE — small and tested")
        verdict = review(TASK, worktree=wt, base="live", runner=yes)
        self.assertTrue(verdict.approved)
        argv = yes.calls[0]
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertIn("--bare", argv)
        self.assertNotIn("Edit", argv[argv.index("--allowedTools") + 1])
        for out in ("VERDICT: REJECT — weakens a test", "I think it is fine", ""):
            with self.subTest(out=out):
                self.assertFalse(review(TASK, worktree=wt, base="live", runner=_Fake(0, out)).approved)
        self.assertFalse(review(TASK, worktree=wt, base="live", runner=_Fake(1, "auth error")).approved)
```

- [ ] **Step 2: Run them and confirm they fail.** Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `upgrade/engines.py`**

```python
"""The two engines: Codex writes the change in its sandbox, Claude reviews it (spec U4, U10)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentic_trading.upgrade.run import Runner
from agentic_trading.upgrade.tasks import Task

ALLOWED_TEXT = (
    "src/agentic_trading/strategies/, src/agentic_trading/swarm/, tests/test_swarm_*.py (except "
    "tests/test_swarm_funding.py and tests/test_swarm_desk.py), tests/test_strategies_*.py, docs/, and only the "
    "test-count numbers in README.md and CONTRIBUTING.md"
)
WRITE_RULES = f"""You are improving one part of an automated trading system that must earn more over time.
Make ONE small, well-tested improvement for the task below.
Rules you must follow:
- You may ONLY create or modify files under: {ALLOWED_TEXT}. Never delete or rename files.
- Never use subprocess, sockets, HTTP, os.environ/os.getenv, eval/exec/__import__, or open files outside the repo.
- Never delete or weaken a test. Write a failing test first, then the code. Keep the change under 12 files and
  400 changed lines. Keep on-disk JSON formats readable by the current code.
- Run: python -m pytest tests -q   (there is no network; the suite must pass). If you add tests, update the test
  count in README.md (3 places) and CONTRIBUTING.md, then run: python tools/check_doc_counts.py
- Do not commit. Do not touch git.
- If the task cannot be done inside the allowed files, change nothing and end with one line:
  NOT_APPLICABLE: <reason>
- Otherwise end with one line: DONE: <one sentence on what changed and why it should earn more>
"""
REVIEW_RULES = f"""You are the second reviewer of an automatic change to a trading system. Be strict.
Approve only if ALL hold: it does the task; it only touches {ALLOWED_TEXT}; tests were added or strengthened and
none weakened; it adds no network, subprocess, environment or out-of-repo file access; it does not change on-disk
JSON formats in a way the previous code could not read; it cannot make the system take more risk than before.
End with exactly one line: VERDICT: APPROVE — <reason>   or   VERDICT: REJECT — <reason>
"""
REVIEW_TOOLS = "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git status:*)"


@dataclass(frozen=True)
class EngineResult:
    status: str
    summary: str


@dataclass(frozen=True)
class Verdict:
    approved: bool
    reason: str


def write(task: Task, *, worktree: Path, scratch: Path, runner: Runner, timeout: float = 2700.0) -> EngineResult:
    last = scratch / "codex-last.txt"
    last.unlink(missing_ok=True)
    prompt = f"{WRITE_RULES}\nTASK: {task.title}\n{task.detail}\n"
    done = runner(["codex", "exec", "-C", str(worktree), "-s", "workspace-write", "--skip-git-repo-check",
                   "--ephemeral", "-o", str(last), prompt], cwd=worktree, timeout=timeout)
    if done.code != 0:
        return EngineResult("failed", f"codex exited {done.code}: {done.out.strip()[-200:]}")
    text = last.read_text(encoding="utf-8") if last.is_file() else done.out
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("NOT_APPLICABLE:"):
            return EngineResult("not_applicable", line[len("NOT_APPLICABLE:"):].strip()[:300])
        if line.startswith("DONE:"):
            return EngineResult("changed", line[len("DONE:"):].strip()[:300])
    return EngineResult("changed", (text.strip().splitlines() or ["no summary"])[-1][:300])


def review(task: Task, *, worktree: Path, base: str, runner: Runner, timeout: float = 1200.0) -> Verdict:
    prompt = (f"{REVIEW_RULES}\nTASK: {task.title}\n{task.detail}\n"
              f"See the change with: git diff --cached {base}\n")
    done = runner(["claude", "-p", prompt, "--bare", "--allowedTools", REVIEW_TOOLS, "--output-format", "text"],
                  cwd=worktree, timeout=timeout)
    if done.code != 0:
        return Verdict(False, f"the reviewer failed (exit {done.code}): {done.out.strip()[-200:]}")
    for line in reversed(done.out.strip().splitlines()):
        if "VERDICT:" in line:
            rest = line.split("VERDICT:", 1)[1].strip()
            return Verdict(rest.upper().startswith("APPROVE"), rest[:300])
    return Verdict(False, "the reviewer gave no verdict")
```

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat(upgrade): Codex writes inside its sandbox, Claude must approve`.

---

### Task 6: Shipping and rolling back

**Files:**
- Create: `src/agentic_trading/upgrade/ship.py`
- Test: `tests/test_upgrade_ship.py`

**Interfaces:**
- Consumes: `Runner` (Task 1).
- **Produces:** `SERVICES`, `IDENTITY`, and `Shipyard(repo, state_dir, runner, live="live")`, whose methods are:
  - `prepare(branch) -> Path`
  - `diff(wt) -> tuple[str, str]`
  - `file_at(path) -> str` (the live version, `""` if new)
  - `commit(wt, title) -> bool`
  - `publish(wt, branch, title, body) -> str` (the PR url, or `""`)
  - `head(path=None) -> str`
  - `deploy(branch, tag) -> bool`
  - `snapshot(tag)`, `restore(tag) -> bool`
  - `rollback(commit) -> tuple[bool, str]`
  - `restart() -> bool`
  - `cleanup(wt)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_upgrade_ship.py
"""Ship forward with a record; roll back by revert (never reset), restoring the swarm's stores."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentic_trading.upgrade.run import RunResult
from agentic_trading.upgrade.ship import SERVICES, Shipyard


class _Fake:
    def __init__(self, fail: str = "") -> None:
        self.calls, self.fail = [], fail

    def __call__(self, argv, cwd=None, timeout=600.0) -> RunResult:
        self.calls.append(list(argv))
        line = " ".join(argv)
        if self.fail and self.fail in line:
            return RunResult(1, "boom")
        if "rev-parse HEAD" in line:
            return RunResult(0, "abc123\n")
        if "pr create" in line:
            return RunResult(0, "https://github.com/x/y/pull/42\n")
        return RunResult(0, "")


class ShipTests(unittest.TestCase):
    def _yard(self, name: str, fail: str = ""):
        state = Path(name) / "state"
        (state / "swarm").mkdir(parents=True)
        (state / "desk").mkdir()
        (state / "swarm" / "population.json").write_text('{"agents": []}')
        (state / "desk" / "swarm.json").write_text('{"cash": "50"}')
        fake = _Fake(fail)
        return Shipyard(Path(name) / "repo", state, fake), fake, state

    def test_deploy_fast_forwards_live_pushes_snapshots_and_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            yard, fake, state = self._yard(name)
            self.assertTrue(yard.deploy("auto/2026-10-07-x", tag="abc123"))
            lines = [" ".join(c) for c in fake.calls]
            self.assertTrue((state / "upgrade" / "snapshots" / "abc123" / "swarm" / "population.json").is_file())
        self.assertTrue(any("merge --ff-only auto/2026-10-07-x" in l for l in lines))
        self.assertTrue(any("push" in l and "live" in l for l in lines))
        self.assertTrue(any(l.startswith("systemctl --user restart") and all(s in l for s in SERVICES) for l in lines))
        self.assertFalse(any("reset --hard" in l for l in lines))

    def test_a_failed_fast_forward_restarts_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            yard, fake, _ = self._yard(name, fail="merge --ff-only")
            self.assertFalse(yard.deploy("auto/x", tag="t"))
        self.assertFalse(any("restart" in " ".join(c) for c in fake.calls))

    def test_rollback_reverts_restores_and_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            yard, fake, state = self._yard(name)
            yard.snapshot("abc123")
            (state / "desk" / "swarm.json").write_text('{"cash": "0"}')  # the new code changed the book
            ok, note = yard.rollback("abc123")
            restored = (state / "desk" / "swarm.json").read_text()
            lines = [" ".join(c) for c in fake.calls]
        self.assertTrue(ok, note)
        self.assertEqual(restored, '{"cash": "50"}')
        self.assertTrue(any("revert --no-edit abc123" in l and "user.name=Hkshoonya" in l for l in lines))
        self.assertFalse(any("reset --hard" in l for l in lines))

    def test_commit_uses_the_owners_identity_and_publish_returns_the_pr(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            yard, fake, _ = self._yard(name)
            wt = Path(name) / "wt"
            self.assertTrue(yard.commit(wt, "Weight contributors by evidence"))
            url = yard.publish(wt, "auto/x", "Weight contributors by evidence", "body")
        commit = " ".join(fake.calls[0])
        self.assertIn("user.name=Hkshoonya", commit)
        self.assertIn("auto-upgrade: Weight contributors by evidence", commit)
        self.assertNotIn("Claude", commit)
        self.assertEqual(url, "https://github.com/x/y/pull/42")
```

- [ ] **Step 2: Run them and confirm they fail.** Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `upgrade/ship.py`**

```python
"""Ship a change forward with a record, and take it back by revert (spec U5, U7).

The live checkout's tracked ``data/bars`` files change locally all day, so nothing here ever
resets the tree. Forward is a fast-forward of ``live``; backward is a ``git revert`` commit,
plus a restore of the swarm's stores from the snapshot taken just before the deploy.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from agentic_trading.upgrade.run import Runner

IDENTITY = ["-c", "user.name=Hkshoonya", "-c", "user.email=154622641+Hkshoonya@users.noreply.github.com"]
SERVICES = ("agentic-trading", "agentic-trading-dashboard", "agentic-trading-venues")
STORES = ("swarm", "desk/swarm.json")  # the editable modules' data, relative to state_dir


class Shipyard:
    def __init__(self, repo: Path, state_dir: Path, runner: Runner, live: str = "live") -> None:
        self.repo, self.state_dir, self.runner, self.live = Path(repo), Path(state_dir), runner, live

    def _git(self, *args: str, where: Optional[Path] = None, timeout: float = 300.0):
        return self.runner(["git", "-C", str(where or self.repo), *args], timeout=timeout)

    def prepare(self, branch: str) -> Path:
        wt = self.repo.parent / "agentic-trading-auto"
        self._git("worktree", "remove", "--force", str(wt))
        self._git("worktree", "prune")
        self._git("branch", "-D", branch)
        self._git("worktree", "add", "-b", branch, str(wt), self.live)
        return wt

    def diff(self, wt: Path) -> tuple[str, str]:
        self._git("add", "-A", where=wt)
        names = self._git("diff", "--cached", "--name-status", "-M", self.live, where=wt).out
        unified = self._git("diff", "--cached", "-U0", self.live, where=wt).out
        return names, unified

    def file_at(self, path: str) -> str:
        shown = self._git("show", f"{self.live}:{path}")
        return shown.out if shown.code == 0 else ""

    def head(self, path: Optional[Path] = None) -> str:
        return self._git("rev-parse", "HEAD", where=path).out.strip()

    def commit(self, wt: Path, title: str) -> bool:
        return self.runner(["git", *IDENTITY, "-C", str(wt), "commit", "-q", "-m", f"auto-upgrade: {title}"],
                           timeout=120.0).code == 0

    def publish(self, wt: Path, branch: str, title: str, body: str) -> str:
        if self._git("push", "-q", "-u", "origin", branch, where=wt, timeout=300.0).code != 0:
            return ""
        made = self.runner(["gh", "pr", "create", "--base", self.live, "--head", branch,
                            "--title", f"[auto-upgrade] {title}", "--body", body], cwd=wt, timeout=120.0)
        return made.out.strip().splitlines()[-1] if made.code == 0 and made.out.strip() else ""

    def snapshot(self, tag: str) -> None:
        target = self.state_dir / "upgrade" / "snapshots" / tag
        for store in STORES:
            source = self.state_dir / store
            if source.is_dir():
                shutil.copytree(source, target / store, dirs_exist_ok=True)
            elif source.is_file():
                (target / store).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target / store)

    def restore(self, tag: str) -> bool:
        source = self.state_dir / "upgrade" / "snapshots" / tag
        if not source.is_dir():
            return False
        for store in STORES:
            saved = source / store
            if saved.is_dir():
                shutil.copytree(saved, self.state_dir / store, dirs_exist_ok=True)
            elif saved.is_file():
                shutil.copy2(saved, self.state_dir / store)
        return True

    def restart(self) -> bool:
        return self.runner(["systemctl", "--user", "restart", *SERVICES], timeout=180.0).code == 0

    def deploy(self, branch: str, tag: str) -> bool:
        self.snapshot(tag)
        if self._git("merge", "--ff-only", branch).code != 0:
            return False
        self._git("push", "-q", "origin", self.live, timeout=300.0)
        return self.restart()

    def rollback(self, commit: str) -> tuple[bool, str]:
        reverted = self.runner(["git", *IDENTITY, "-C", str(self.repo), "revert", "--no-edit", commit], timeout=300.0)
        if reverted.code != 0:
            return False, f"git revert failed: {reverted.out.strip()[-200:]}"
        self._git("push", "-q", "origin", self.live, timeout=300.0)
        restored = self.restore(commit)
        if not self.restart():
            return False, "reverted, but the services did not restart"
        return True, "reverted and restarted" + ("" if restored else " (no data snapshot to restore)")

    def cleanup(self, wt: Path) -> None:
        self._git("worktree", "remove", "--force", str(wt))
```

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat(upgrade): ship by fast-forward with a PR record; roll back by revert plus a store restore`.

---
### Task 7: The watchdog (canary and rollback), plus its alert

**Files:**
- Create: `src/agentic_trading/upgrade/watchdog.py`
- Modify: `src/agentic_trading/notify.py`. Add to `ALERT_EVENTS` `"upgrade_rolled_back": (0.0, "critical")` and `"upgrade_rollback_failed": (0.0, "critical")`, and give `alert_for` one branch returning `Alert(key=event, title="Automatic upgrade rolled back" or "Upgrade rollback FAILED", body=str(record.get("reason"))[:300], urgency="critical")`.
- Test: `tests/test_upgrade_watchdog.py`

**Interfaces:**
- Consumes: `Control`, `load_control`, `save_control` (Task 1); `Shipyard` (Task 6); `Task`, `record`, `note_health` (Task 4).
- **Produces:**
  - `checks(runner, *, journal_dir, since, now, http_get) -> list[str]`
  - `watch(*, state_dir, journal_dir, now, runner, shipyard, journal, notify, http_get) -> str`, which returns one of `idle`, `watching`, `passed`, `rolled_back`, `rollback_failed` or `nothing to roll back`

**What `checks` looks at:**
- `systemctl --user is-active <svc>` must print `active` for each of `SERVICES`;
- `journalctl --user -u <svc> --since <since> --no-pager -o cat` must not contain `Traceback (most recent call last)`;
- 15 minutes after the deploy, today's trader journal (`<journal_dir>/<UTC date>.jsonl`) must have been modified within the last 15 minutes;
- `http_get("http://127.0.0.1:8787/api/desk" | "/api/swarm" | "/api/fast")` must return 200;
- `systemctl --user show agentic-trading-swarm.service -p Result --value` must print `success` or nothing.

**What `watch` does:**
1. If `rollback_requested` is set, it rolls back the open canary or `last_shipped`. If neither exists, it clears the request and returns `nothing to roll back`.
2. With an open canary and problems found, it rolls back and calls `note_health` for each problem.
3. With an open canary, no problems, and `now >= until`, it closes the canary and journals `upgrade_canary_passed`.

**A rollback** calls `shipyard.rollback(target["commit"])`.
- **On success:** the canary, `last_shipped` and the request are cleared; it pauses with the reason `rolled back: <why>`; the task is recorded as `rolled_back` (by `target["task"]`, `target["title"]`); and `upgrade_rolled_back` is journaled and notified.
- **On failure:** it pauses with `rollback failed: <note>`, then journals and notifies `upgrade_rollback_failed`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_upgrade_watchdog.py
"""The canary: healthy passes after 24 h; any failure, or the operator, rolls back and pauses."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_trading.upgrade.control import load_control, request_rollback, save_control
from agentic_trading.upgrade.run import RunResult
from agentic_trading.upgrade.watchdog import checks, watch

T0 = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)


class _Runner:
    def __init__(self, inactive: str = "", traceback: str = "") -> None:
        self.inactive, self.traceback = inactive, traceback

    def __call__(self, argv, cwd=None, timeout=600.0) -> RunResult:
        line = " ".join(argv)
        if "is-active" in line:
            return RunResult(0 if self.inactive not in line or not self.inactive else 3,
                             "inactive" if self.inactive and self.inactive in line else "active")
        if "journalctl" in line and self.traceback and self.traceback in line:
            return RunResult(0, "Traceback (most recent call last):\n  boom")
        if "show agentic-trading-swarm" in line:
            return RunResult(0, "success")
        return RunResult(0, "")


class _Yard:
    def __init__(self, ok: bool = True) -> None:
        self.ok, self.rolled = ok, []

    def rollback(self, commit: str):
        self.rolled.append(commit)
        return self.ok, "reverted and restarted" if self.ok else "git revert failed"


def _canary(state: Path, started: datetime) -> None:
    control = load_control(state)
    control.canary = {"commit": "abc", "task": "k", "title": "t", "started_at": started.isoformat(),
                      "until": (started + timedelta(hours=24)).isoformat()}
    control.last_shipped = dict(control.canary)
    save_control(state, control)


class WatchdogTests(unittest.TestCase):
    def _watch(self, state, journal, now, runner, yard, http=lambda url: 200):
        events, alerts = [], []
        result = watch(state_dir=state, journal_dir=journal, now=now, runner=runner, shipyard=yard,
                       journal=events.append, notify=alerts.append, http_get=http)
        return result, events, alerts

    def test_checks_name_each_problem(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            journal = Path(name)
            (journal / f"{T0:%Y-%m-%d}.jsonl").write_text("{}\n")
            fine = checks(_Runner(), journal_dir=journal, since=T0, now=T0 + timedelta(minutes=5),
                          http_get=lambda url: 200)
            self.assertEqual(fine, [])
            bad = checks(_Runner(inactive="agentic-trading-venues", traceback="agentic-trading-dashboard"),
                         journal_dir=journal, since=T0, now=T0 + timedelta(minutes=5),
                         http_get=lambda url: 500 if url.endswith("/api/swarm") else 200)
        self.assertEqual(len(bad), 3)

    def test_idle_without_a_canary(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            self.assertEqual(self._watch(Path(name), Path(name), T0, _Runner(), _Yard())[0], "idle")

    def test_a_failing_canary_rolls_back_pauses_and_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            _canary(state, T0)
            yard = _Yard()
            result, events, alerts = self._watch(state, state, T0 + timedelta(minutes=5),
                                                 _Runner(inactive="agentic-trading"), yard)
            control = load_control(state)
        self.assertEqual((result, yard.rolled), ("rolled_back", ["abc"]))
        self.assertTrue(control.paused)
        self.assertIn("rolled back", control.reason)
        self.assertEqual(control.canary, {})
        self.assertEqual([e["event"] for e in alerts], ["upgrade_rolled_back"])

    def test_a_healthy_canary_passes_after_24_hours(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            (state / f"{T0:%Y-%m-%d}.jsonl").write_text("{}\n")
            late = state / f"{T0 + timedelta(hours=25):%Y-%m-%d}.jsonl"
            late.write_text("{}\n")
            stamp = (T0 + timedelta(hours=25)).timestamp()
            os.utime(late, (stamp, stamp))  # the trader wrote its journal just now (test clock)
            _canary(state, T0)
            self.assertEqual(self._watch(state, state, T0 + timedelta(minutes=5), _Runner(), _Yard())[0], "watching")
            result, events, _ = self._watch(state, state, T0 + timedelta(hours=25), _Runner(), _Yard())
            self.assertEqual(result, "passed")
            self.assertEqual(load_control(state).canary, {})
            self.assertEqual(load_control(state).last_shipped["commit"], "abc")

    def test_the_operator_rolls_back_the_last_shipped_change(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            _canary(state, T0)
            control = load_control(state)
            control.canary = {}
            save_control(state, control)  # the canary passed; the change is still the last shipped
            request_rollback(state)
            yard = _Yard()
            result, _, _ = self._watch(state, state, T0 + timedelta(days=3), _Runner(), yard)
            self.assertEqual((result, yard.rolled), ("rolled_back", ["abc"]))
            request_rollback(state)
            again, _, _ = self._watch(state, state, T0 + timedelta(days=3), _Runner(), _Yard())
        self.assertEqual(again, "nothing to roll back")  # never reverts something else

    def test_a_failed_rollback_pauses_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            _canary(state, T0)
            result, _, alerts = self._watch(state, state, T0 + timedelta(minutes=5), _Runner(inactive="agentic-trading"),
                                            _Yard(ok=False))
            control = load_control(state)
        self.assertEqual(result, "rollback_failed")
        self.assertIn("rollback failed", control.reason)
        self.assertEqual(alerts[0]["event"], "upgrade_rollback_failed")
```

- [ ] **Step 2: Run them and confirm they fail.** Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `upgrade/watchdog.py`**

```python
"""The canary: every 5 minutes, check that an upgrade broke nothing; if it did, or the operator
asks, take it back and pause (spec U6, U8). It checks crashes, not decisions."""

from __future__ import annotations

import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from agentic_trading.upgrade.control import load_control, save_control
from agentic_trading.upgrade.run import Runner
from agentic_trading.upgrade.ship import SERVICES
from agentic_trading.upgrade.tasks import Task, note_health, record

GRACE = timedelta(minutes=15)
APIS = ("/api/desk", "/api/swarm", "/api/fast")


def http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # loopback only
            return int(response.status)
    except Exception:  # noqa: BLE001 — any failure is "not 200"
        return 0


def checks(runner: Runner, *, journal_dir: Path, since: datetime, now: datetime,
           http_get: Callable[[str], int]) -> list[str]:
    problems = []
    stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    for service in SERVICES:
        if runner(["systemctl", "--user", "is-active", service], timeout=30.0).out.strip() != "active":
            problems.append(f"{service} is not active")
        logs = runner(["journalctl", "--user", "-u", service, "--since", stamp, "--no-pager", "-o", "cat"],
                      timeout=60.0).out
        if "Traceback (most recent call last)" in logs:
            problems.append(f"{service} logged a traceback")
    if now - since >= GRACE:
        today = Path(journal_dir) / f"{now.astimezone(timezone.utc):%Y-%m-%d}.jsonl"
        fresh = today.is_file() and now.timestamp() - today.stat().st_mtime < GRACE.total_seconds()
        if not fresh:
            problems.append("the trader has not written its journal for 15 minutes")
    for path in APIS:
        if http_get(f"http://127.0.0.1:8787{path}") != 200:
            problems.append(f"the dashboard's {path} does not answer")
    result = runner(["systemctl", "--user", "show", "agentic-trading-swarm.service", "-p", "Result", "--value"],
                    timeout=30.0).out.strip()
    if result not in ("success", ""):
        problems.append(f"the last swarm step ended with {result}")
    return problems


def watch(*, state_dir: Path, journal_dir: Path, now: datetime, runner: Runner, shipyard: Any,
          journal: Callable[[dict], None], notify: Callable[[dict], Any],
          http_get: Callable[[str], int] = http_status) -> str:
    control = load_control(state_dir)
    if control.rollback_requested:
        target = control.canary or control.last_shipped
        if not target:
            control.rollback_requested = False
            save_control(state_dir, control)
            return "nothing to roll back"
        return _rollback(state_dir, control, target, "the operator asked for a rollback", now, shipyard, journal,
                         notify)
    if not control.canary:
        return "idle"
    since = datetime.fromisoformat(control.canary["started_at"])
    problems = checks(runner, journal_dir=journal_dir, since=since, now=now, http_get=http_get)
    if problems:
        for problem in problems:
            note_health(state_dir, problem)
        return _rollback(state_dir, control, control.canary, "; ".join(problems), now, shipyard, journal, notify)
    if now >= datetime.fromisoformat(control.canary["until"]):
        title = control.canary.get("title", "")
        control.canary = {}
        save_control(state_dir, control)
        journal({"event": "upgrade_canary_passed", "at": now.isoformat(), "text": f"the canary passed: {title}"})
        return "passed"
    return "watching"


def _rollback(state_dir: Path, control: Any, target: dict, why: str, now: datetime, shipyard: Any,
              journal: Callable[[dict], None], notify: Callable[[dict], Any]) -> str:
    ok, note = shipyard.rollback(str(target["commit"]))
    if not ok:
        control.paused, control.reason = True, f"rollback failed: {note}"
        save_control(state_dir, control)
        event = {"event": "upgrade_rollback_failed", "at": now.isoformat(), "reason": f"{why} — {note}",
                 "text": f"rolling back {target.get('title', '')} FAILED: {note}"}
        journal(event)
        notify(event)
        return "rollback_failed"
    control.canary, control.last_shipped, control.rollback_requested = {}, {}, False
    control.paused, control.reason = True, f"rolled back: {why}"[:300]
    save_control(state_dir, control)
    record(state_dir, Task(str(target.get("task", "")), str(target.get("title", "")), "", "upgrade"),
           "rolled_back", now, why)
    event = {"event": "upgrade_rolled_back", "at": now.isoformat(), "reason": why,
             "text": f"rolled back {target.get('title', '')}: {why}"[:300]}
    journal(event)
    notify(event)
    return "rolled_back"
```

- [ ] **Step 4: Implement the alert, then run the tests.** Add the two `ALERT_EVENTS` entries and an `alert_for` branch in `notify.py`, placed before its final `return`:

```python
    if event in ("upgrade_rolled_back", "upgrade_rollback_failed"):
        return Alert(key=event, title="Automatic upgrade rolled back" if event == "upgrade_rolled_back"
                     else "Upgrade rollback FAILED", body=str(record.get("reason") or "")[:300], urgency=urgency)
```

Run: `... -m pytest tests/test_upgrade_watchdog.py tests/test_notify*.py -q`. Expected: PASS.

- [ ] **Step 5: Commit:** `feat(upgrade): the canary watchdog — rolls back and pauses on any failure or on request; alerts`.

---
### Task 8: The daily cycle and the `upgrade` CLI

**Files:**
- Create: `src/agentic_trading/upgrade/cycle.py`, `upgrade/cli.py`
- Modify: `src/agentic_trading/cli.py` (register beside `add_swarm_parser`/`dispatch_swarm`), `windows/AgenticTrader.spec` (add `"agentic_trading.upgrade.cli"`)
- Test: `tests/test_upgrade_cycle.py`

**Interfaces:**
- Consumes: everything in Tasks 1–7.
- **Produces:**
  - `Outcome(code: int, message: str)`
  - `run_cycle(*, state_dir, journal_dir, repo, venv, settings, mode, now, runner, shipyard, write, review, probe_paths) -> Outcome`. `probe_paths` is `(secrets_path, home_file)`; `mode` is `"shadow"` or `"live"`.
  - It writes `state/upgrade.json` as `{"enabled", "updated_at", "last": {"at", "outcome", "message", "task"}}`.
  - `add_upgrade_parser(sub)` and `dispatch_upgrade(args) -> int`.

**The order inside a cycle:**
1. **Refusals (U11):** disabled, paused, canary open, kill switch (`state/risk_guard.json` `kill_switch`), not on `live`, dirty outside `data/`, sandbox probe failing.
2. **Pick and prepare:** `pick` (none: "nothing to work on"); prepare the worktree; count the tests before.
3. **Write:** if the engine reports not-applicable, record `out_of_scope`; if it fails, record `failed`.
4. **Check:** take the diff (no changes counts as `failed`); run the policy against the trusted closure; count the tests after; run the suite; run the doc counts; get the review.
5. **Pause check:** if paused now, abandon with no failure recorded.
6. **Ship:** commit, publish, deploy (tagged with the candidate's commit), open the canary, record `shipped`, clean up.

Every outcome is journaled to `upgrade-<day>.jsonl` (`FastJournal(prefix="upgrade")`) and written to `upgrade.json`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_upgrade_cycle.py
"""One daily cycle: every refusal stops it before anything changes; a clean run ships with a canary."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agentic_trading.upgrade.control import load_control, pause
from agentic_trading.upgrade.cycle import run_cycle
from agentic_trading.upgrade.engines import EngineResult, Verdict
from agentic_trading.upgrade.run import RunResult
from agentic_trading.upgrade.settings import UpgradeConfig

NOW = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)
ON = UpgradeConfig(enabled=True)
SWARM_FILE = "src/agentic_trading/swarm/blend.py"


class _Runner:
    """git/bwrap stand-in: a clean live checkout, a sandbox that holds, a suite that passes."""

    def __init__(self, branch="live", dirty="", probe_leaks=False, suite_code=0) -> None:
        self.branch, self.dirty, self.leaks, self.suite_code, self.calls = branch, dirty, probe_leaks, suite_code, []
        self.counted = 0

    def __call__(self, argv, cwd=None, timeout=600.0) -> RunResult:
        self.calls.append(list(argv))
        line = " ".join(argv)
        if "rev-parse --abbrev-ref HEAD" in line:
            return RunResult(0, self.branch + "\n")
        if "status --porcelain" in line:
            return RunResult(0, self.dirty)
        if "socket.create_connection" in line:  # the sandbox probe
            return RunResult(0, json.dumps({"network": self.leaks, "secrets": False, "home": False,
                                            "write_outside": False}))
        if "--collect-only" in line:
            self.counted += 1
            return RunResult(0, "10 tests collected\n" if self.counted == 1 else "11 tests collected\n")
        if "pytest" in line:
            return RunResult(self.suite_code, "passed" if self.suite_code == 0 else "1 failed")
        return RunResult(0, "")


class _Yard:
    def __init__(self, root: Path, change: str = SWARM_FILE) -> None:
        self.root, self.change, self.calls = root, change, []

    def prepare(self, branch):
        self.calls.append(("prepare", branch))
        wt = self.root / "wt"
        (wt / Path(self.change).parent).mkdir(parents=True, exist_ok=True)
        (wt / self.change).write_text("X = 2\n")
        return wt

    def diff(self, wt):
        return f"M\t{self.change}\n", f"diff --git a/{self.change} b/{self.change}\n-X = 1\n+X = 2\n"

    def file_at(self, path):
        return "X = 1\n"

    def head(self, path=None):
        return "abc123"

    def commit(self, wt, title):
        self.calls.append(("commit", title))
        return True

    def publish(self, wt, branch, title, body):
        self.calls.append(("publish", branch))
        return "https://github.com/x/y/pull/7"

    def deploy(self, branch, tag):
        self.calls.append(("deploy", branch, tag))
        return True

    def cleanup(self, wt):
        self.calls.append(("cleanup",))


class CycleTests(unittest.TestCase):
    def _run(self, state, *, runner=None, yard=None, settings=ON, mode="shadow", write=None, review=None):
        (state / "proposals.json").write_text(json.dumps({"proposals": [
            {"title": "Better shares", "category": "strategy", "confidence": 0.9, "status": "proposed",
             "change": "c"}]}))
        repo = state / "repo"
        (repo / "src" / "agentic_trading").mkdir(parents=True, exist_ok=True)
        return run_cycle(state_dir=state, journal_dir=state / "journal", repo=repo, venv=state / "venv",
                         settings=settings, mode=mode, now=NOW, runner=runner or _Runner(),
                         shipyard=yard or _Yard(state),
                         write=write or (lambda task, **kw: EngineResult("changed", "shares grow")),
                         review=review or (lambda task, **kw: Verdict(True, "small and tested")),
                         probe_paths=(state / "secrets.toml", state / "home.txt"))

    def test_each_refusal_changes_nothing(self) -> None:
        cases = {
            "off": dict(settings=UpgradeConfig(enabled=False)),
            "not on live": dict(runner=_Runner(branch="feat/x")),
            "uncommitted": dict(runner=_Runner(dirty=" M src/agentic_trading/risk.py\n")),
            "sandbox": dict(runner=_Runner(probe_leaks=True)),
        }
        for needle, kw in cases.items():
            with self.subTest(needle=needle), tempfile.TemporaryDirectory() as name:
                yard = _Yard(Path(name))
                outcome = self._run(Path(name), yard=yard, **kw)
                self.assertIn(needle, outcome.message)
                self.assertEqual(yard.calls, [])
        with tempfile.TemporaryDirectory() as name:
            pause(Path(name), "operator")
            self.assertIn("paused", self._run(Path(name)).message)
        with tempfile.TemporaryDirectory() as name:
            (Path(name) / "risk_guard.json").write_text('{"kill_switch": true}')
            self.assertIn("kill switch", self._run(Path(name)).message)

    def test_data_changes_alone_are_not_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            outcome = self._run(Path(name), runner=_Runner(dirty=" M data/bars/SPY_day.jsonl\n"))
        self.assertIn("shipped", outcome.message)

    def test_a_clean_run_ships_and_opens_a_canary(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            yard = _Yard(state)
            outcome = self._run(state, yard=yard)
            control = load_control(state)
            status = json.loads((state / "upgrade.json").read_text())
        self.assertEqual(outcome.code, 0)
        self.assertEqual([c[0] for c in yard.calls], ["prepare", "commit", "publish", "deploy", "cleanup"])
        self.assertEqual(control.canary["commit"], "abc123")
        self.assertEqual(control.last_shipped["pr"], "https://github.com/x/y/pull/7")
        self.assertEqual(status["last"]["outcome"], "shipped")

    def test_each_failure_stops_before_shipping(self) -> None:
        cases = {
            "policy": dict(yard_change="src/agentic_trading/risk.py"),
            "tests": dict(runner=_Runner(suite_code=1)),
            "review": dict(review=lambda task, **kw: Verdict(False, "weakens a test")),
            "out of scope": dict(write=lambda task, **kw: EngineResult("not_applicable", "runtime.py")),
            "live mode": dict(mode="live"),
        }
        for needle, kw in cases.items():
            with self.subTest(needle=needle), tempfile.TemporaryDirectory() as name:
                state = Path(name)
                yard = _Yard(state, change=kw.pop("yard_change", SWARM_FILE))
                self._run(state, yard=yard, **kw)
                self.assertNotIn("deploy", [c[0] for c in yard.calls])
                self.assertEqual(load_control(state).canary, {})

    def test_pause_pressed_mid_run_stops_before_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            yard = _Yard(state)

            def reviewer(task, **kw):
                pause(state, "operator pressed pause during review")
                return Verdict(True, "fine")

            outcome = self._run(state, yard=yard, review=reviewer)
        self.assertIn("paused", outcome.message)
        self.assertNotIn("deploy", [c[0] for c in yard.calls])
```

- [ ] **Step 2: Run them and confirm they fail.** Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `upgrade/cycle.py`**

```python
"""One daily upgrade: refuse unless every guard holds, then write, check, test, review, ship, watch."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from agentic_trading import jsonio
from agentic_trading.fast.service import FastJournal
from agentic_trading.upgrade import engines as engine_module
from agentic_trading.upgrade.control import load_control, save_control
from agentic_trading.upgrade.policy import check, control_closure, parse_diff
from agentic_trading.upgrade.run import Runner
from agentic_trading.upgrade.sandbox import count_tests, probe, run_doc_counts, run_suite
from agentic_trading.upgrade.settings import UpgradeConfig
from agentic_trading.upgrade.tasks import Task, pick, record


@dataclass(frozen=True)
class Outcome:
    code: int
    message: str


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "change"


def run_cycle(*, state_dir: Path, journal_dir: Path, repo: Path, venv: Path, settings: UpgradeConfig, mode: str,
              now: datetime, runner: Runner, shipyard: Any, write: Callable[..., Any] = engine_module.write,
              review: Callable[..., Any] = engine_module.review, probe_paths: tuple[Path, Path]) -> Outcome:
    journal = FastJournal(journal_dir, prefix="upgrade")
    task_holder: dict[str, Task] = {}

    def finish(code: int, outcome: str, message: str) -> Outcome:
        task = task_holder.get("task")
        journal.append({"event": f"upgrade_{outcome}", "at": now.isoformat(), "text": message,
                        "task": task.title if task else ""})
        path = Path(state_dir) / "upgrade.json"
        jsonio.write_text(path, jsonio.dumps({"enabled": settings.enabled, "updated_at": now.isoformat(), "last": {
            "at": now.isoformat(), "outcome": outcome, "message": message,
            "task": task.title if task else ""}}, indent=2) + "\n")
        return Outcome(code, message)

    if not settings.enabled:
        return finish(0, "off", "the upgrader is off ([upgrade] enabled = false)")
    control = load_control(state_dir)
    if control.paused:
        return finish(0, "paused", f"paused: {control.reason}")
    if control.canary:
        return finish(0, "waiting", f"a canary is open until {control.canary.get('until')}")
    try:
        guard = json.loads((Path(state_dir) / "risk_guard.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        guard = {}
    if guard.get("kill_switch"):
        return finish(0, "refused", "the kill switch is engaged")
    branch_now = runner(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], timeout=30.0).out.strip()
    if branch_now != "live":
        return finish(1, "refused", f"the live checkout is on {branch_now or '?'}, not on live")
    dirty = [line for line in runner(["git", "-C", str(repo), "status", "--porcelain"], timeout=60.0).out.splitlines()
             if line.strip() and not line[3:].startswith("data/")]
    if dirty:
        return finish(1, "refused", f"the live checkout has uncommitted changes outside data/: {dirty[0][3:]}")
    problems = probe(runner, worktree=repo, venv=venv, secrets=probe_paths[0], home_file=probe_paths[1])
    if problems:
        return finish(1, "refused", "the sandbox is not safe: " + "; ".join(problems))

    task = pick(state_dir, now)
    if task is None:
        return finish(0, "idle", "nothing to work on")
    task_holder["task"] = task
    branch = f"auto/{now:%Y-%m-%d}-{_slug(task.title)}"
    wt = shipyard.prepare(branch)

    def fail(outcome: str, message: str, failure: bool = True) -> Outcome:
        record(state_dir, task, "failed" if failure else outcome, now, message)
        shipyard.cleanup(wt)
        return finish(0, outcome, message)

    tests_before = count_tests(runner, worktree=wt, venv=venv)
    scratch = Path(state_dir) / "upgrade"
    scratch.mkdir(parents=True, exist_ok=True)
    written = write(task, worktree=wt, scratch=scratch, runner=runner)
    if written.status == "not_applicable":
        record(state_dir, task, "out_of_scope", now, written.summary)
        shipyard.cleanup(wt)
        return finish(0, "out_of_scope", f"{task.title}: out of scope ({written.summary})")
    if written.status != "changed":
        return fail("failed", f"{task.title}: the writer failed ({written.summary})")
    names, unified = shipyard.diff(wt)
    changes = parse_diff(names, unified)
    if not changes:
        return fail("failed", f"{task.title}: the writer changed nothing")
    before = {c.path: shipyard.file_at(c.old_path or c.path) for c in changes if c.path.endswith(".py")}
    after = {c.path: (wt / c.path).read_text(encoding="utf-8") if (wt / c.path).is_file() else ""
             for c in changes if c.path.endswith(".py")}
    tests_after = count_tests(runner, worktree=wt, venv=venv)
    reasons = check(changes, live=(mode == "live"), closure=control_closure(Path(repo) / "src"), before=before,
                    after=after, tests_before=tests_before or 0, tests_after=tests_after or 0,
                    max_files=settings.max_files, max_lines=settings.max_lines)
    if tests_before is None or tests_after is None:
        reasons.append("the tests could not be counted")
    if reasons:
        return fail("refused", f"{task.title}: refused by the walls: " + "; ".join(reasons))
    suite = run_suite(runner, worktree=wt, venv=venv)
    if suite.code != 0:
        return fail("failed", f"{task.title}: the tests failed: {suite.out.strip()[-300:]}")
    docs = run_doc_counts(runner, worktree=wt, venv=venv)
    if docs.code != 0:
        return fail("failed", f"{task.title}: the doc counts disagree: {docs.out.strip()[-200:]}")
    verdict = review(task, worktree=wt, base="live", runner=runner)
    if not verdict.approved:
        return fail("rejected", f"{task.title}: the reviewer rejected it: {verdict.reason}")
    if load_control(state_dir).paused:
        shipyard.cleanup(wt)
        return finish(0, "paused", f"paused before deploying {task.title}")
    if not shipyard.commit(wt, task.title):
        return fail("failed", f"{task.title}: the commit failed")
    body = (f"Automatic upgrade.\n\nTask: {task.title}\n\n{task.detail}\n\nWriter: {written.summary}\n"
            f"Reviewer: {verdict.reason}\nTests: {tests_before} → {tests_after}, all passing in the sandbox.")
    pr = shipyard.publish(wt, branch, task.title, body)
    commit = shipyard.head(wt)
    if not shipyard.deploy(branch, tag=commit):
        return fail("failed", f"{task.title}: the deploy failed; live is unchanged")
    control = load_control(state_dir)
    control.canary = {"commit": commit, "branch": branch, "task": task.key, "title": task.title, "pr": pr,
                      "started_at": now.isoformat(),
                      "until": (now + timedelta(hours=settings.canary_hours)).isoformat()}
    control.last_shipped = dict(control.canary)
    save_control(state_dir, control)
    record(state_dir, task, "shipped", now, written.summary)
    shipyard.cleanup(wt)
    return finish(0, "shipped", f"shipped {task.title} ({pr or 'no PR'}); canary open for "
                                f"{settings.canary_hours} h")
```

- [ ] **Step 4: Implement `upgrade/cli.py` and register it**

```python
"""``agentic-trading upgrade``: run today's upgrade, watch the canary, or flip the switches."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ACTIONS = ("run", "watch", "pause", "resume", "rollback", "status")


def add_upgrade_parser(sub: Any) -> None:
    parser = sub.add_parser("upgrade", help="The self-upgrader: run, watch, pause, resume, rollback, status")
    actions = parser.add_subparsers(dest="upgrade_action", required=True)
    for name in ACTIONS:
        actions.add_parser(name).add_argument("--config", required=True)


def dispatch_upgrade(args: Any) -> int:
    from agentic_trading.config import load_config
    from agentic_trading.journal import DecisionJournal
    from agentic_trading.upgrade import control as switches
    from agentic_trading.upgrade.run import real_runner
    from agentic_trading.upgrade.settings import load_upgrade_config
    from agentic_trading.upgrade.ship import Shipyard

    config = load_config(args.config)
    state, journal_dir = Path(config.state_dir), Path(config.journal_dir)
    repo = Path(args.config).resolve().parent.parent
    action = args.upgrade_action
    if action in ("pause", "resume", "rollback"):
        {"pause": lambda: switches.pause(state, "paused from the command line"),
         "resume": lambda: switches.resume(state), "rollback": lambda: switches.request_rollback(state)}[action]()
        DecisionJournal(journal_dir).append({"event": "upgrade_control", "action": action, "source": "cli"})
        print(f"upgrade {action}: done" + (" (the watchdog rolls back within 5 minutes)" if action == "rollback" else ""))
        return 0
    if action == "status":
        control = switches.load_control(state)
        try:
            last = json.loads((state / "upgrade.json").read_text(encoding="utf-8")).get("last") or {}
        except (OSError, ValueError):
            last = {}
        print(f"paused: {control.paused} {control.reason}".rstrip())
        print(f"canary: {control.canary.get('title', '-')} until {control.canary.get('until', '-')}")
        print(f"last: {last.get('outcome', '-')} — {last.get('message', '-')}")
        return 0
    now = datetime.now(timezone.utc)
    yard = Shipyard(repo, state, real_runner)
    if action == "watch":
        from agentic_trading.notify import build_notifier
        from agentic_trading.upgrade.watchdog import watch
        from agentic_trading.fast.service import FastJournal

        notifier = build_notifier()
        result = watch(state_dir=state, journal_dir=journal_dir, now=now, runner=real_runner, shipyard=yard,
                       journal=FastJournal(journal_dir, prefix="upgrade").append,
                       notify=(notifier.dispatch if notifier else (lambda record: None)))
        print(f"upgrade watch: {result}")
        return 0
    from agentic_trading.upgrade.cycle import run_cycle

    mode_file = state / "mode"
    mode = mode_file.read_text().strip() if mode_file.is_file() else str(config.mode)
    outcome = run_cycle(state_dir=state, journal_dir=journal_dir, repo=repo, venv=repo / ".venv",
                        settings=load_upgrade_config(args.config), mode=mode, now=now, runner=real_runner,
                        shipyard=yard, probe_paths=(repo / "config" / "secrets.toml", Path.home() / ".bashrc"))
    print(f"upgrade run: {outcome.message}")
    return outcome.code
```

In `cli.py`, after `add_swarm_parser(sub)`, add:
```python
    from agentic_trading.upgrade.cli import add_upgrade_parser

    add_upgrade_parser(sub)
```
After the `swarm` dispatch, add:
```python
    if args.command == "upgrade":
        from agentic_trading.upgrade.cli import dispatch_upgrade

        return dispatch_upgrade(args)
```

- [ ] **Step 5: Run the tests and the suite.** Expected: PASS.

- [ ] **Step 6: Commit:** `feat(upgrade): the daily cycle (every refusal first, pause checked before deploy) and the upgrade CLI`.

---
### Task 9: The Evolution card and the one-click buttons

**Files:**
- Create: `src/agentic_trading/dashboard_upgrade.py`
- Modify: `dashboard.py`. Add a `DashboardState.upgrade()` method, the `GET /api/upgrade` route beside `/api/swarm`, and `POST /api/upgrade` inside `do_POST`, behind the same fences.
- Modify: `dashboard_html.py` (the card after the Swarm card), `dashboard_css.py`, `dashboard_cockpit_js.py`.
- Test: `tests/test_dashboard_upgrade.py`; add a line to `tests/test_cockpit_page.py`.

**Interfaces:**
- Consumes: `load_control`, `pause`, `resume`, `request_rollback` (Task 1); `state/upgrade.json` (Task 8).
- **Produces:**
  - `upgrade_view(state_dir) -> dict` with `paused`, `reason`, `rollback_requested`, `canary{title, until, pr}`, `last_shipped{title, pr}`, `last{at, outcome, message, task}` and `enabled`;
  - `POST /api/upgrade {"action": "pause"|"resume"|"rollback"}`, which returns the view;
  - `CockpitFmt.upgradeLine(view)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dashboard_upgrade.py
"""The Evolution card: the state, and three fenced buttons that only flip switches."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agentic_trading.dashboard_upgrade import upgrade_view
from agentic_trading.upgrade.control import load_control, pause


def _post(port: int, body: dict, *, content_type: str = "application/json", origin: str | None = None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/upgrade", data=json.dumps(body).encode(),
                                     method="POST", headers={"Content-Type": content_type,
                                                             **({"Origin": origin} if origin else {})})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, {}


class UpgradeViewTests(unittest.TestCase):
    def test_the_view_whitelists_and_reads_the_switches(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            pause(state, "operator")
            (state / "upgrade.json").write_text(json.dumps({"enabled": True, "secret": "LEAK", "last": {
                "at": "t", "outcome": "shipped", "message": "m", "task": "x", "token": "LEAK"}}))
            view = upgrade_view(state)
        self.assertNotIn("LEAK", json.dumps(view))
        self.assertEqual((view["paused"], view["enabled"], view["last"]["outcome"]), (True, True, "shipped"))

    def test_the_buttons_flip_switches_behind_the_fences(self) -> None:
        from agentic_trading.config import load_config
        from agentic_trading.dashboard import serve
        from tests.test_runtime_daemon import _write_config

        with tempfile.TemporaryDirectory() as name:
            config = load_config(_write_config(Path(name)))
            server = serve(config, host="127.0.0.1", port=0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            port = server.server_address[1]
            try:
                self.assertEqual(_post(port, {"action": "pause"})[0], 200)
                self.assertTrue(load_control(config.state_dir).paused)
                self.assertEqual(_post(port, {"action": "rollback"})[0], 200)
                self.assertTrue(load_control(config.state_dir).rollback_requested)
                self.assertEqual(_post(port, {"action": "resume"})[0], 200)
                self.assertFalse(load_control(config.state_dir).paused)
                self.assertEqual(_post(port, {"action": "deploy"})[0], 400)
                self.assertEqual(_post(port, {"action": "pause"}, content_type="text/plain")[0], 415)
                self.assertEqual(_post(port, {"action": "pause"}, origin="http://evil.example")[0], 403)
            finally:
                server.shutdown()
                server.server_close()
```

Add to the page tests: `self.assertIn('id="upgrade"', _section("strategies"))`, and add a JS test calling `CockpitFmt.upgradeLine(...)`:
- `{paused: true, reason: "rolled back: x"}` gives `"paused — rolled back: x"`;
- `{canary: {title: "T", until: "2026-10-08T02:00:00+00:00"}}` gives a string starting `"watching T until"`;
- `{enabled: false}` gives `"off — set [upgrade] enabled = true"`;
- otherwise `"running daily"`.

- [ ] **Step 2: Run them and confirm they fail.**

- [ ] **Step 3: Implement**

`dashboard_upgrade.py`:
```python
"""The self-upgrader as the console sees it: named fields only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentic_trading.upgrade.control import load_control

LAST_FIELDS = ("at", "outcome", "message", "task")


def upgrade_view(state_dir: Path | str) -> dict[str, Any]:
    control = load_control(state_dir)
    try:
        status = json.loads((Path(state_dir) / "upgrade.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    last = status.get("last") if isinstance(status.get("last"), dict) else {}
    pick = lambda raw, names: {n: raw.get(n) for n in names} if raw else {}  # noqa: E731
    return {"enabled": bool(status.get("enabled")), "paused": control.paused, "reason": control.reason,
            "rollback_requested": control.rollback_requested,
            "canary": pick(control.canary, ("title", "until", "pr")),
            "last_shipped": pick(control.last_shipped, ("title", "pr")),
            "last": {n: last.get(n) for n in LAST_FIELDS}}
```

In `dashboard.py`:
- Add the `upgrade()` method: `return upgrade_view(self.state_dir)`, after `refresh_config()`.
- Add the GET route: `if parsed.path == "/api/upgrade": self._json(self.state.upgrade()); return`.
- In `do_POST`:
  - change the path test to `parsed.path not in ("/api/arm", "/api/disarm", "/api/upgrade")`;
  - change the loopback message to `"console writes are local-only"`;
  - after the payload is read, add:
    ```python
    if parsed.path == "/api/upgrade":
        action = str(payload.get("action", "")).strip().lower()
        moves = {"pause": lambda: upgrade_control.pause(self.state.state_dir, "paused from the console"),
                 "resume": lambda: upgrade_control.resume(self.state.state_dir),
                 "rollback": lambda: upgrade_control.request_rollback(self.state.state_dir)}
        if action not in moves:
            self._json({"error": 'action must be "pause", "resume" or "rollback"'}, status=400)
            return
        moves[action]()
        self._journal_upgrade(action)
        self._json(self.state.upgrade())
        return
    ```
  - add `_journal_upgrade(action)`, which appends `{"event": "upgrade_control", "action": action, "source": "console"}` through the same journal `_journal_arming` uses;
  - import `from agentic_trading.upgrade import control as upgrade_control` and `from agentic_trading.dashboard_upgrade import upgrade_view`.

`dashboard_html.py`, after the Swarm card:
```html
<div class="card span12"><h2>Evolution · the system upgrading itself (Codex writes, Claude reviews)</h2><div id="upgrade" class="swarm"><div class="sub">reading the upgrader…</div></div></div>
```
`dashboard_css.py`:
```css
.ubtns{display:flex;gap:8px;flex-wrap:wrap}
.ubtns button{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:6px 12px;cursor:pointer}
.ubtns button.stop{border-color:var(--sell)}
```
`dashboard_cockpit_js.py`:
- add `upgradeLine` to `CockpitFmt`:
  ```javascript
    const upgradeLine = (v) => !v || v.enabled === false ? 'off — set [upgrade] enabled = true'
      : v.paused ? 'paused — ' + (v.reason || 'by the operator')
      : v.canary && v.canary.title ? 'watching ' + v.canary.title + ' until ' + v.canary.until
      : 'running daily';
  ```
  Export it, add `renderUpgrade` and `pollUpgrade`, and start the poll every 30 s in `start()`:
  ```javascript
    function renderUpgrade(view) {
      const box = $('upgrade');
      if (!box || !view) return;
      const last = view.last || {};
      const shipped = view.last_shipped && view.last_shipped.title
        ? '<div class="sub">' + esc('last shipped: ' + view.last_shipped.title) + (view.last_shipped.pr
          ? ' · <a href="' + esc(view.last_shipped.pr) + '" target="_blank" rel="noopener">PR</a>' : '') + '</div>' : '';
      box.innerHTML = '<div class="fhead"><span class="fbadge">' + esc(CockpitFmt.upgradeLine(view)) + '</span></div>'
        + shipped + (last.message ? '<div class="sub">' + esc('latest: ' + last.message) + '</div>' : '')
        + '<div class="ubtns">' + (view.paused ? '<button data-act="resume">Resume</button>'
          : '<button class="stop" data-act="pause">Pause</button>')
        + '<button class="stop" data-act="rollback">Roll back last upgrade</button></div>';
      box.querySelectorAll('button[data-act]').forEach((b) => b.addEventListener('click', async () => {
        const act = b.getAttribute('data-act');
        if (act === 'rollback' && !window.confirm('Roll back the last automatic upgrade?')) return;
        try {
          const r = await fetch('/api/upgrade', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: act }) });
          renderUpgrade(await r.json());
        } catch (e) { /* the next poll shows the truth */ }
      }));
    }

    async function pollUpgrade() {
      if (document.hidden) return;
      try {
        const r = await fetch('/api/upgrade');
        if (r.ok) renderUpgrade(await r.json());
      } catch (e) { /* keep the last picture */ }
    }
  ```

- [ ] **Step 4: Run the tests and the suite.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat(dashboard): the Evolution card with fenced one-click Pause, Resume and Roll back`.

---

### Task 10: The follow gap (paper vs account)

**Files:**
- Modify: `src/agentic_trading/desk/desk.py` (`_retarget`, `_save`, `_load`), `src/agentic_trading/dashboard_swarm.py`, and the swarm card JS (`swarmHead` gains the gap).
- Test: add to `tests/test_swarm_funding.py` and `tests/test_dashboard_swarm.py`.

**Interfaces:**
- **Produces:**
  - `StrategyDesk.follow: list[float]` (the last 60 gaps, in bp), saved in `desk.json["follow"]`;
  - `swarm_view(...)["follow_gap_bps"]`: the median, or `None`;
  - `["follow_count"]`.

**Rule (P4):** in a scoped `_retarget(only=...)`, for each symbol in `only` that a funded read-only member holds with a known book price `p`, where the live mid `m` is known, append `round((m / p - 1) * 10000, 1)`. Keep the last 60.

- [ ] **Step 1: Write the failing tests.** Add to `FundingTests`:

```python
    def test_a_follow_records_the_gap_from_the_swarms_paper_price(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            rig = _Rig(Path(name), 0.5)
            _write_swarm(rig.path, {"ETH-USD": "1"}, {"ETH-USD": "20"})
            rig.later_quote("ETH-USD", "20.1", "20.3")  # mid 20.2: 1% above the swarm's close
            gaps = list(rig.desk.follow)
            saved = json.loads((Path(name) / "desk" / "desk.json").read_text())["follow"]
        self.assertEqual(gaps, [100.0])
        self.assertEqual(saved, [100.0])
```

Add to `tests/test_dashboard_swarm.py`:

```python
    def test_the_follow_gap_is_the_median_of_the_desks_record(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            _state(Path(name), T0)
            (Path(name) / "desk").mkdir()
            (Path(name) / "desk" / "desk.json").write_text(json.dumps({"allocations": {}, "follow": [10, -30, 50]}))
            view = swarm_view(Path(name), [], now=T0)
        self.assertEqual((view["follow_gap_bps"], view["follow_count"]), (10, 3))
```

- [ ] **Step 2: Run them and confirm they fail.**

- [ ] **Step 3: Implement**

In `desk.py`:
- add `self.follow: list[float] = []` in `__init__`, before `self._load()`;
- in `_retarget`, inside the `if only is not None:` block and before `targets = scoped`, add:
  ```python
  for member in self._funded_read_only():
      for symbol in only & set(member.book.positions):
          book_price, live = member.book.prices.get(symbol), prices.get(symbol)
          if book_price and live:
              self.follow.append(round(float((live / book_price - 1) * 10000), 1))
  self.follow = self.follow[-60:]
  ```
- in `_save`'s payload, add `"follow": self.follow`;
- in `_load`, add `self.follow = [float(x) for x in raw.get("follow") or []][-60:]`.

In `dashboard_swarm.py`, `swarm_view` reads `desk/desk.json` once and adds:
```python
    "follow_gap_bps": statistics.median(follow) if follow else None, "follow_count": len(follow),
```
`follow` is the saved list (empty on any read error).

In the JS `swarmHead`, append `' · follows at ' + signedPct(view.follow_gap_bps / 100) + ' vs paper'` when `view.follow_gap_bps != null`.

- [ ] **Step 4: Run the tests and the suite.** Expected: PASS.

- [ ] **Step 5: Commit:** `feat(desk): measure how far the account's follows drift from the swarm's paper prices`.

---

### Task 11: Timers, docs, counts

**Files:**
- Create:
  - `deploy/agentic-trading-upgrade` (a launcher running `upgrade run --config config/agentic.toml`);
  - `deploy/agentic-trading-upgrade.service` (`Type=oneshot`, `TimeoutStartSec=90min`, `Nice=10`, logging to `data/state/upgrade.log`);
  - `deploy/agentic-trading-upgrade.timer` (`OnCalendar=*-*-* 02:00:00 UTC`, `Persistent=true`);
  - `deploy/agentic-trading-upgrade-watch` (a launcher running `upgrade watch ...`);
  - `deploy/agentic-trading-upgrade-watch.service` (oneshot, 10 min);
  - `deploy/agentic-trading-upgrade-watch.timer` (`OnBootSec=5min`, `OnUnitActiveSec=5min`).
- Modify: `README.md` (an "Evolution (self-upgrader)" section: what it may change, the walls, the buttons, the AppArmor step), `CLAUDE.md` (layout: `upgrade/`; hard rule: the `live` branch, and never switch the main checkout), and the test counts.
- Test: `tests/test_upgrade_deploy.py` checks the two timers' `OnCalendar`/`OnUnitActiveSec` and `Persistent`, and that the launchers call `upgrade run` and `upgrade watch`.

Write the units with the same shape as `deploy/agentic-trading-swarm{,.service,.timer}`: launcher `cd`s into the repo, `exec .venv/bin/agentic-trading ...`. Run the suite and `tools/check_doc_counts.py`, then commit: `build(upgrade): daily and watchdog timers, and the evolution docs`.

---

### Task 12: Launch (operational)

Do this only after the whole-branch review passes and its fixes are in.

1. Push `feat/self-upgrade`, open the PR (base `feat/swarm-funding`), and run the Windows build until it is green.
2. **Create the `live` branch:** in the main checkout, `git switch feat/self-upgrade`, then `git branch live`, `git push -u origin live`, `git switch live`. Remove the worktree first; Git refuses to check out a branch another worktree holds. From now on, all work branches from `live` in worktrees.
3. **Config:** back up `config/agentic.toml` to `.bak-upgrade`, then add `[upgrade]` with `enabled = true`.
4. **Timers:** install the four units and both launchers into `~/.local/bin`, then `daemon-reload` and `enable --now` both timers.
5. **Verify the engines without shipping anything:**
   - `claude -p --bare "Reply with exactly: OK"` must print `OK`. If `--bare` can't authenticate, rule on the closest flags that still skip hooks and memory, then record and test them.
   - `codex exec --help` must work.
6. **First run:** `agentic-trading upgrade run --config config/agentic.toml`. On this machine it must stop at the sandbox probe (`the sandbox is not safe: the sandbox would not start ... RTM_NEWADDR`) until the user adds the AppArmor profile. That refusal is the correct, safe outcome. Record it.
7. **Restart and check:**
   - restart the dashboard;
   - `curl /api/upgrade`;
   - press **Pause** and **Resume** on the card through Playwright, confirming `control.json` flips;
   - run the size sweep.
8. **The user's one step,** given in the final message:
   ```bash
   sudo tee /etc/apparmor.d/bwrap >/dev/null <<'EOF'
   abi <abi/4.0>,
   include <tunables/global>
   profile bwrap /usr/bin/bwrap flags=(unconfined) {
     userns,
     include if exists <local/bwrap>
   }
   EOF
   sudo systemctl reload apparmor
   ```
   After it, the next 02:00 UTC run passes the probe and starts upgrading.
9. Update memory.

---

## Self-review notes

**Spec coverage:**

| Spec item | Where |
|---|---|
| U1–U3 | Task 2 |
| U4 | Tasks 5 and 8 |
| U5 | Tasks 6 and 8 |
| U6 | Task 7 |
| U7 | Task 6 |
| U8 | Tasks 1, 8 and 9 |
| U9 | Task 4 |
| U10 | Tasks 3 and 5 |
| U11 | Task 8 |
| U12 | Tasks 3, 8 and 12 |
| Follow gap | Task 10 |
| Operations | Tasks 11 and 12 |

**Deviations:** none. The CLI's watch path builds the notifier with `notify.build_notifier()`, as the trader does.
