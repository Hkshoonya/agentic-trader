"""Panic-reversion rule: fires on capitulation, exits on recovery or time."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from agentic_trading.history import Bar
from agentic_trading.panic import CANDIDATES, _split, sigma_dip_trades


def series(closes: list[float]) -> list[Bar]:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    bars: list[Bar] = []
    for index, close in enumerate(closes):
        price = Decimal(f"{close:.4f}")
        bars.append(
            Bar(
                "TEST",
                start + timedelta(days=index),
                price,
                price,
                price,
                price,
            )
        )
    return bars


class SigmaDipTests(unittest.TestCase):
    def test_enters_on_a_capitulation_bar_and_exits(self) -> None:
        # 30 calm days, then a violent down day, then recovery.
        closes = [100 + (0.1 if i % 2 else -0.1) for i in range(30)]
        closes += [88.0, 95.0, 99.0, 101.0, 102.0]
        trades = sigma_dip_trades(
            series(closes), sigma_window=20, sigma_k=2.0, max_hold=5, recovery=True
        )
        self.assertGreaterEqual(len(trades), 1)
        trade = trades[0]
        self.assertIn(trade.exit_reason, ("recovery", "timeout"))
        # Bought well below the pre-crash level (costs push the fill slightly
        # above the 95.00 open, so compare against the calm-market price).
        self.assertLess(trade.entry_price, Decimal("98"))

    def test_does_not_trade_a_quiet_market(self) -> None:
        closes = [100 + (0.05 if i % 2 else -0.05) for i in range(40)]
        self.assertEqual(
            sigma_dip_trades(series(closes), sigma_window=20, sigma_k=2.0), []
        )

    def test_short_history_is_ignored(self) -> None:
        self.assertEqual(sigma_dip_trades(series([100, 99, 98]), sigma_window=20), [])

    def test_candidate_grid_covers_the_hypothesis_space(self) -> None:
        self.assertEqual(len(CANDIDATES), 3 * 4 * 3 * 2)
        self.assertTrue(all(c["sigma_k"] > 0 for c in CANDIDATES))

    def test_split_keeps_bars_chronological(self) -> None:
        bars = series([float(100 + i) for i in range(100)])
        train, test = _split({"TEST": bars}, 0.7)
        self.assertEqual(len(train["TEST"]), 70)
        self.assertEqual(len(test["TEST"]), 30)
        self.assertLess(train["TEST"][-1].start, test["TEST"][0].start)


if __name__ == "__main__":
    unittest.main()
