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

    def test_a_failed_revert_is_aborted_and_restarts_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            yard, fake, _ = self._yard(name, fail="revert --no-edit")
            ok, note = yard.rollback("abc123")
            lines = [" ".join(c) for c in fake.calls]
        self.assertFalse(ok)
        self.assertIn("git revert failed", note)
        self.assertTrue(any("revert --abort" in l for l in lines))  # never leave live mid-revert
        self.assertFalse(any("restart" in l for l in lines))

    def test_a_rollback_after_the_canary_keeps_the_data(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            yard, fake, state = self._yard(name)
            yard.snapshot("abc123")
            (state / "desk" / "swarm.json").write_text('{"cash": "0"}')  # days of healthy life since
            ok, note = yard.rollback("abc123", restore=False)
            kept = (state / "desk" / "swarm.json").read_text()
        self.assertTrue(ok, note)
        self.assertEqual(kept, '{"cash": "0"}')

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
