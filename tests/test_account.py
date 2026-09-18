"""Runtime and money: the numbers an operator checks first.

Both must survive a restart, or they reset exactly when they matter — and P&L
must say which snapshot it is measured against, because "profit" without an
origin is a number nobody can act on.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_trading import account


class UptimeTests(unittest.TestCase):
    def test_a_first_session_starts_the_clock(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
            payload = account.note_session_start(state, now=now)
        self.assertEqual(payload["sessions"], 1)
        self.assertEqual(payload["total_runtime_seconds"], 0.0)
        self.assertEqual(payload["session_started_at"], now.isoformat())

    def test_a_restart_credits_the_previous_session(self) -> None:
        """Ten minutes of work before a restart is ten minutes of runtime."""
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            first = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
            account.note_session_start(state, now=first)
            account.note_equity(state, "50", now=first)
            account.note_heartbeat(state, now=first + timedelta(minutes=10))
            payload = account.note_session_start(
                state, now=first + timedelta(minutes=12)
            )
        self.assertEqual(payload["sessions"], 2)
        self.assertAlmostEqual(payload["total_runtime_seconds"], 600.0, places=1)

    def test_an_unclean_stop_still_counts_up_to_the_last_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            first = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
            account.note_session_start(state, now=first)
            account.note_heartbeat(state, now=first + timedelta(minutes=3))
            payload = account.note_session_start(
                state, now=first + timedelta(hours=5)
            )
        self.assertAlmostEqual(payload["total_runtime_seconds"], 180.0, places=1)

    def test_totals_add_the_current_session_to_the_accumulated_total(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            start = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
            account.note_session_start(state, now=start - timedelta(minutes=30))
            account.note_heartbeat(state, now=start)
            account.note_session_start(state, now=start)
            view = account.totals(state, "50", now=start + timedelta(seconds=90))
        self.assertAlmostEqual(view["runtime"]["session_seconds"], 90.0, places=1)
        self.assertAlmostEqual(view["runtime"]["total_seconds"], 30 * 60 + 90, places=1)


class PnlTests(unittest.TestCase):
    def test_the_first_equity_becomes_the_all_time_origin(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            account.note_equity(state, "50")
            account.note_equity(state, "80")  # later reads must not move it
            view = account.totals(state, "80")
        self.assertEqual(view["equity"]["first"], "50")
        self.assertEqual(view["pnl"]["all_time"], "30.00")

    def test_an_implausible_reading_does_not_reset_the_origin(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            account.note_equity(state, "50")
            account.note_equity(state, "0")
            view = account.totals(state, "50")
        self.assertEqual(view["equity"]["first"], "50")

    def test_arming_snapshots_the_balance_once_per_arming(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            account.note_equity(state, "50")
            account.note_armed(state, "50", armed=True, at="2026-09-18T12:00:00+00:00", source="auto")
            # Reading again (the console polls) must not move the snapshot.
            account.note_armed(state, "75", armed=True, at="2026-09-18T12:00:00+00:00", source="auto")
            view = account.totals(state, "75")
        self.assertEqual(view["equity"]["at_arm"], "50")
        self.assertEqual(view["pnl"]["since_arming"], "25.00")

    def test_a_new_arming_takes_a_new_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            account.note_equity(state, "50")
            account.note_armed(state, "50", armed=True, at="t1", source="auto")
            account.note_armed(state, "50", armed=False, at="t2", source="auto")
            account.note_armed(state, "60", armed=True, at="t3", source="auto")
            view = account.totals(state, "65")
        self.assertEqual(view["equity"]["at_arm"], "60")
        self.assertEqual(view["pnl"]["since_arming"], "5.00")
        self.assertEqual(view["equity"]["armed_source"], "auto")

    def test_every_pnl_number_says_what_it_is_measured_against(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            view = account.totals(Path(name), "50")
        self.assertIn("first equity", view["labels"]["all_time"])
        self.assertIn("armed", view["labels"]["since_arming"])

    def test_no_equity_reading_yields_no_invented_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            view = account.totals(Path(name), "0")
        self.assertIsNone(view["pnl"]["all_time"])
        self.assertIsNone(view["pnl"]["since_arming"])


class DashboardAccountViewTests(unittest.TestCase):
    def test_the_view_separates_shadow_pnl_from_equity(self) -> None:
        """Adding simulated fills to a real balance is worse than no number."""
        import json as _json

        from agentic_trading.dashboard import _account_view

        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            account.note_equity(state, "50")
            (state / "risk_guard.json").write_text(
                _json.dumps(
                    {
                        "current_equity": "52",
                        "baseline_equity": "50",
                        "shadow_realized_today": "0.40",
                        "shadow_realized_total": "3.10",
                    }
                )
            )
            view = _account_view(state, "52")
        self.assertEqual(view["pnl"]["today"], "2.00")
        self.assertEqual(view["pnl"]["all_time"], "2.00")
        self.assertIn("never added", view["shadow"]["note"])
        self.assertEqual(view["shadow"]["realized_total"], "3.10")


if __name__ == "__main__":
    unittest.main()
