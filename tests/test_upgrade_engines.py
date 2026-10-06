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
