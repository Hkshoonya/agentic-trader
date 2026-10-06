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


class AlertTests(unittest.TestCase):
    def test_rollbacks_reach_the_operator(self) -> None:
        from agentic_trading.notify import alert_for

        rolled = alert_for({"event": "upgrade_rolled_back", "reason": "agentic-trading is not active"})
        failed = alert_for({"event": "upgrade_rollback_failed", "reason": "git revert failed"})
        self.assertEqual((rolled.title, rolled.urgency, rolled.body),
                         ("Automatic upgrade rolled back", "critical", "agentic-trading is not active"))
        self.assertEqual(failed.title, "Upgrade rollback FAILED")
