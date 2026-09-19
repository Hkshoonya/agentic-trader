"""A measured round trip must survive the passes that recompute slippage.

The first real measurement on the live account (200 bps, $0.10 on a $5 XLM
round trip) vanished within a minute: the execution-cost pass rebuilds the
report from scratch each cycle and wrote its own empty round-trip list over the
top. A measurement that the next tick erases is worse than none, because the
console keeps showing the assumption.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentic_trading import execution


class CarryForwardTests(unittest.TestCase):
    def test_recomputed_slippage_keeps_the_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            execution.record_round_trip(
                state, symbol="XLM-USD", buy_notional=5.00, sell_notional=4.90
            )
            existing = execution.load_report(state)
            assert existing is not None

            # What the runtime does every pass: a fresh slippage report.
            rebuilt = execution.measure(
                [], {}, assumed_per_side_bps=2.0, existing=existing
            )
            execution.save_report(state, rebuilt)

            reloaded = execution.load_report(state)
            assert reloaded is not None
            self.assertEqual(len(reloaded.round_trips), 1)
            self.assertAlmostEqual(
                reloaded.measured_round_trip_bps, 200.0, places=3
            )
            self.assertAlmostEqual(
                execution.measured_cost_usd(state) or 0, 0.10, places=6
            )

    def test_a_pass_without_an_existing_report_starts_empty(self) -> None:
        report = execution.measure([], {}, assumed_per_side_bps=2.0)
        self.assertEqual(report.round_trips, [])
        self.assertIsNone(report.measured_round_trip_bps)


if __name__ == "__main__":
    unittest.main()
