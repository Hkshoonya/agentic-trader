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
