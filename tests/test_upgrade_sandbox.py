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
