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
