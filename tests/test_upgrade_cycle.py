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
from agentic_trading.upgrade.tasks import extend_backlog

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
    def __init__(self, root: Path, change: str = SWARM_FILE, settled: bool = True, link: bool = False,
                 whole: bool = True) -> None:
        self.root, self.change, self.calls, self.settled, self.link = root, change, [], settled, link
        self.whole = whole

    def intact(self, wt):
        return self.whole

    def gitdir(self, wt):
        return self.root / "repo" / ".git" / "worktrees" / "wt"

    def prepare(self, branch):
        self.calls.append(("prepare", branch))
        wt = self.root / "wt"
        (wt / Path(self.change).parent).mkdir(parents=True, exist_ok=True)
        if self.link:
            (wt / self.change).symlink_to(self.root / "elsewhere.py")
        else:
            (wt / self.change).write_text("X = 2\n")
        return wt

    def settle(self, wt):
        self.calls.append(("settle",))
        return self.settled

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
        self.calls.append(("publish", branch, body))
        return "https://github.com/x/y/pull/7"

    def deploy(self, branch, tag):
        self.calls.append(("deploy", branch, tag))
        return True

    def cleanup(self, wt):
        self.calls.append(("cleanup",))


def _no_plan(**kw):
    raise AssertionError("the planner ran when it was not due")


def _journal(state: Path) -> list[dict]:
    return [json.loads(line) for path in sorted((state / "journal").glob("upgrade-*.jsonl"))
            for line in path.read_text().splitlines()]


class CycleTests(unittest.TestCase):
    def _run(self, state, *, runner=None, yard=None, settings=ON, mode="shadow", write=None, review=None, plan=None):
        (state / "proposals.json").write_text(json.dumps({"proposals": [
            {"title": "Better shares", "category": "strategy", "confidence": 0.9, "status": "proposed",
             "change": "c"}]}))
        repo = state / "repo"
        (repo / "src" / "agentic_trading").mkdir(parents=True, exist_ok=True)
        return run_cycle(state_dir=state, journal_dir=state / "journal", repo=repo, venv=state / "venv",
                         settings=settings, mode=mode, now=NOW, runner=runner or _Runner(),
                         shipyard=yard or _Yard(state),
                         writers=(("codex", write or (lambda task, **kw: EngineResult("changed", "shares grow"))),),
                         review=review or (lambda task, **kw: Verdict(True, "small and tested")),
                         plan=plan or _no_plan, probe_paths=(state / "secrets.toml", state / "home.txt"))

    def test_the_reviewer_sees_only_what_the_walls_checked(self) -> None:
        for needle, yard_kw in (("changed the worktree", dict(settled=False)), ("symlink", dict(link=True)),
                                ("git link", dict(whole=False))):
            with self.subTest(needle=needle), tempfile.TemporaryDirectory() as name:
                reviewed = []
                yard = _Yard(Path(name), **yard_kw)
                outcome = self._run(Path(name), yard=yard,
                                    review=lambda task, **kw: reviewed.append(1) or Verdict(True, "ok"))
                self.assertIn(needle, outcome.message)
                self.assertEqual(reviewed, [])
                self.assertFalse(any(c[0] in ("commit", "deploy") for c in yard.calls))

    def test_the_probe_sandbox_never_mounts_the_live_checkout(self) -> None:
        # Mounting the live checkout would put config/secrets.toml inside the sandbox, and a correct
        # probe would then refuse every run.
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            runner = _Runner()
            repo = state / "repo"
            (repo / "src" / "agentic_trading").mkdir(parents=True)
            run_cycle(state_dir=state, journal_dir=state / "journal", repo=repo, venv=repo / ".venv",
                      settings=ON, mode="shadow", now=NOW, runner=runner, shipyard=_Yard(state),
                      writers=(("codex", lambda task, **kw: EngineResult("changed", "x")),),
                      review=lambda task, **kw: Verdict(True, "ok"),
                      probe_paths=(repo / "config" / "secrets.toml", state / "home.txt"))
            secrets = repo / "config" / "secrets.toml"
        argv = next(c for c in runner.calls if "socket.create_connection" in " ".join(c))
        mounted = [Path(argv[i + 1]) for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")]
        self.assertTrue(mounted)
        for source in mounted:
            self.assertFalse(secrets.is_relative_to(source), source)

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
        self.assertEqual([c[0] for c in yard.calls], ["prepare", "settle", "commit", "publish", "deploy", "cleanup"])
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

    def test_an_engine_out_of_quota_charges_the_task_nothing(self) -> None:
        cases = {
            "writer": dict(write=lambda task, **kw: EngineResult("unavailable", "try again at Oct 18th")),
            "reviewer": dict(review=lambda task, **kw: Verdict(False, "usage limit reached", available=False)),
        }
        for engine, kw in cases.items():
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as name:
                state = Path(name)
                yard = _Yard(state)
                outcome = self._run(state, yard=yard, **kw)
                status = json.loads((state / "upgrade.json").read_text())
                attempts = state / "upgrade" / "attempts.json"
                self.assertEqual(outcome.code, 0)
                self.assertIn(f"the {engine} is unavailable", outcome.message)
                self.assertEqual(status["last"]["outcome"], "unavailable")
                self.assertFalse(attempts.exists() and json.loads(attempts.read_text()))
                self.assertNotIn("deploy", [c[0] for c in yard.calls])
                self.assertEqual(yard.calls[-1][0], "cleanup")

    def _chain(self, state: Path, *results: str, yard=None):
        """Writers that answer in turn with the given statuses; returns who was asked."""
        asked = []

        def writer(name, status):
            return lambda task, **kw: asked.append(name) or EngineResult(status, f"{name} said {status}")

        names = ("codex", "deepseek", "claude")
        writers = tuple((names[i], writer(names[i], status)) for i, status in enumerate(results))
        (state / "proposals.json").write_text(json.dumps({"proposals": [
            {"title": "Better swarm shares", "category": "strategy", "confidence": 0.9, "status": "proposed",
             "change": "c"}]}))
        repo = state / "repo"
        (repo / "src" / "agentic_trading").mkdir(parents=True, exist_ok=True)
        outcome = run_cycle(state_dir=state, journal_dir=state / "journal", repo=repo, venv=state / "venv",
                            settings=ON, mode="shadow", now=NOW, runner=_Runner(), shipyard=yard or _Yard(state),
                            writers=writers, review=lambda task, **kw: Verdict(True, "ok"),
                            probe_paths=(state / "secrets.toml", state / "home.txt"))
        return outcome, asked

    def _charged(self, state: Path) -> bool:
        path = state / "upgrade" / "attempts.json"
        return path.exists() and any(e.get("failures") for e in json.loads(path.read_text()).values())

    def test_the_next_writer_takes_over_on_a_fresh_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            yard = _Yard(state)
            outcome, asked = self._chain(state, "unavailable", "failed", "changed", yard=yard)
            body = next(c for c in yard.calls if c[0] == "publish")
            events = [json.loads(line) for f in (state / "journal").glob("*.jsonl") for line in f.read_text().splitlines()]
        self.assertEqual(asked, ["codex", "deepseek", "claude"])
        self.assertIn("shipped", outcome.message)
        self.assertEqual([c[0] for c in yard.calls].count("prepare"), 3)
        self.assertIn("Writer (claude): claude said changed", body[-1])
        self.assertEqual([e["engine"] for e in events if e["event"] == "upgrade_writer"],
                         ["codex", "deepseek", "claude"])

    def test_a_timeout_or_a_real_answer_ends_the_chain(self) -> None:
        for first, stops_as in (("timed_out", "failed"), ("not_applicable", "out_of_scope")):
            with self.subTest(first=first), tempfile.TemporaryDirectory() as name:
                state = Path(name)
                outcome, asked = self._chain(state, first, "changed")
                status = json.loads((state / "upgrade.json").read_text())
            self.assertEqual(asked, ["codex"])
            self.assertEqual(status["last"]["outcome"], stops_as)

    def test_only_the_last_writer_tried_decides_the_charge(self) -> None:
        for results, outcome_word, charged in ((("unavailable", "unavailable", "unavailable"), "unavailable", False),
                                               (("failed", "unavailable", "unavailable"), "unavailable", False),
                                               (("unavailable", "unavailable", "failed"), "failed", True)):
            with self.subTest(results=results), tempfile.TemporaryDirectory() as name:
                state = Path(name)
                outcome, asked = self._chain(state, *results)
                status = json.loads((state / "upgrade.json").read_text())
                self.assertEqual(len(asked), 3)
                self.assertEqual(status["last"]["outcome"], outcome_word)
                self.assertEqual(self._charged(state), charged)
                self.assertIn("tried codex", outcome.message)

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

    def test_the_planner_runs_when_the_queue_is_low_and_its_task_is_picked(self) -> None:
        def plan(*, state_dir, now, cwd, **kw):
            plan.calls.append(cwd)
            row = {"key": "plan-1", "title": "Blend swarm shares by evidence", "detail": "d", "origin": "planner"}
            extend_backlog(state_dir, [row])
            return {"at": now.isoformat(), "added": [row], "skipped": ["x: a near-duplicate"], "error": ""}
        plan.calls = []
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            (state / "upgrade").mkdir()
            (state / "upgrade" / "backlog.json").write_text("[]")
            outcome = self._run(state, plan=plan)
            events = _journal(state)
        self.assertEqual(plan.calls, [state / "upgrade" / "probe-root"])
        planned = [e for e in events if e["event"] == "upgrade_plan"][0]
        self.assertEqual(planned["added"], ["Blend swarm shares by evidence"])
        self.assertEqual(planned["skipped"], ["x: a near-duplicate"])
        self.assertIn("shipped Blend swarm shares by evidence", outcome.message)

    def test_a_failed_plan_is_journaled_and_the_run_goes_on(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            (state / "upgrade").mkdir()
            (state / "upgrade" / "backlog.json").write_text("[]")
            outcome = self._run(state, plan=lambda **kw: {"added": [], "skipped": [], "error": "timed out"})
            events = _journal(state)
        self.assertEqual([e["error"] for e in events if e["event"] == "upgrade_plan"], ["timed out"])
        self.assertEqual(outcome.message, "nothing to work on")


class DocCountTests(unittest.TestCase):
    README = "tests-10%20passing\n.venv/bin/python -m pytest tests -q   # 10 tests\nThe swarm breeds agents.\n"

    def _run(self, state: Path, write) -> tuple:
        yard = _Yard(state)
        yard.file_at = lambda path: self.README if path == "README.md" else "X = 1\n"
        outcome = CycleTests._run(CycleTests(), state, yard=yard, write=write)
        return outcome, state / "wt"

    def test_the_writers_doc_edits_are_dropped_and_the_new_count_written(self) -> None:
        def write(task, *, worktree, **kw):
            (worktree / "README.md").write_text(self.README.replace("breeds agents", "breeds BETTER agents"))
            return EngineResult("changed", "shares grow")

        with tempfile.TemporaryDirectory() as name:
            outcome, wt = self._run(Path(name), write)
            self.assertEqual((wt / "README.md").read_text(), self.README.replace("10", "11"))
            self.assertEqual((wt / "CONTRIBUTING.md").read_text(), "X = 1\n")
            self.assertTrue(outcome.message.startswith("shipped"), outcome.message)

    def test_a_doc_file_swapped_for_a_symlink_is_never_written_through(self) -> None:
        def write(task, *, worktree, **kw):
            (worktree / "README.md").symlink_to(worktree.parent / "outside.md")
            return EngineResult("changed", "shares grow")

        with tempfile.TemporaryDirectory() as name:
            outside = Path(name) / "outside.md"
            outside.write_text("untouched\n")
            self._run(Path(name), write)
            self.assertEqual(outside.read_text(), "untouched\n")
