"""The cost the venue actually charges, measured from cash rather than quotes.

The first real round trip on this account — $5.00 of XLM bought and sold back —
moved $5.00 out and $4.90 in. The quoted spread at the time was 8 basis points,
so the quote described a tenth of the cost. Whatever the venue's part is made
of, it is much closer to a fixed charge than a percentage: the same trip on a $1
order would be 10% of the order, which no edge survives.

These tests pin the measurement, its sign convention, and the rule that an order
too small to justify its own cost is refused rather than scaled up.
"""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agentic_trading import execution


class RoundTripMeasurementTests(unittest.TestCase):
    def test_a_real_round_trip_is_priced_from_the_brokers_cash(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            # The runtime always prices against a stated assumption; without one
            # there is nothing to compare the measurement to.
            execution.save_report(
                state, execution.CostReport(assumed_per_side_bps=2.0)
            )
            report = execution.record_round_trip(
                state,
                symbol="XLM-USD",
                buy_notional=5.00,
                sell_notional=4.90,
                quantity=25.28,
                buy_order_id="buy-1",
                sell_order_id="sell-1",
            )
            # Positive means "this cost money" — the same convention the
            # slippage samples use, so a reader never has to remember a sign.
            self.assertAlmostEqual(report.measured_round_trip_bps, 200.0, places=3)
            self.assertAlmostEqual(report.per_side_cost_bps, 100.0, places=3)
            self.assertAlmostEqual(report.ratio, 50.0, places=3)
            self.assertAlmostEqual(execution.measured_cost_usd(state), 0.10, places=6)

    def test_the_median_survives_a_bad_print(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            for buy, sell in ((5.00, 4.90), (5.00, 4.92), (10.00, 6.00)):
                execution.record_round_trip(
                    state, symbol="XLM-USD", buy_notional=buy, sell_notional=sell
                )
            report = execution.load_report(state)
            assert report is not None
            self.assertAlmostEqual(report.measured_round_trip_bps, 200.0, places=3)

    def test_a_profitable_trip_is_not_reported_as_a_cost(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            report = execution.record_round_trip(
                state, symbol="XLM-USD", buy_notional=5.00, sell_notional=5.20
            )
            self.assertLess(report.measured_round_trip_bps, 0)
            # A trip that made money must not raise the floor for everyone else.
            self.assertEqual(execution.measured_cost_usd(state), 0.0)


class CostAwareSizingTests(unittest.TestCase):
    def test_the_smallest_order_that_can_absorb_the_measured_cost(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            self.assertIsNone(
                execution.required_notional_for_cost(state, max_share=0.02),
                "no measurement must not invent a floor",
            )
            execution.record_round_trip(
                state, symbol="XLM-USD", buy_notional=5.00, sell_notional=4.90
            )
            required = execution.required_notional_for_cost(state, max_share=0.02)
            self.assertAlmostEqual(float(required or 0), 5.00, places=6)
            # A tighter tolerance demands a bigger order: 1% of $10 is $0.10.
            tighter = execution.required_notional_for_cost(state, max_share=0.01)
            self.assertAlmostEqual(float(tighter or 0), 10.00, places=6)

    def test_the_rule_can_be_switched_off(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            execution.record_round_trip(
                state, symbol="XLM-USD", buy_notional=5.00, sell_notional=4.90
            )
            self.assertIsNone(
                execution.required_notional_for_cost(state, max_share=0)
            )


if __name__ == "__main__":
    unittest.main()
