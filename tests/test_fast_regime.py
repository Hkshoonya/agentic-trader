"""Ticks become minute bars; bars become one of five market readings."""

from __future__ import annotations

import unittest
from datetime import timedelta

from agentic_trading.fast.bars import MinuteBars
from agentic_trading.fast.regime import (
    CHOPPY, SQUEEZE, TRENDING, UNCLEAR, WARMING, efficiency_ratio, read_regime,
)
from tests.fast_support import T0, bars_from


class BarTests(unittest.TestCase):
    def test_a_minute_rollover_returns_the_completed_bar(self) -> None:
        bars = MinuteBars()
        for seconds, price in ((5, 100.0), (30, 101.0), (50, 99.0)):
            self.assertIsNone(bars.add("BTC/USD", price, T0 + timedelta(seconds=seconds)))
        done = bars.add("BTC/USD", 100.5, T0 + timedelta(minutes=1, seconds=2))
        self.assertEqual((done.minute, done.open, done.high, done.low, done.close), (T0, 100.0, 101.0, 99.0, 99.0))
        self.assertEqual(len(bars.closed("BTC/USD")), 1)

    def test_a_late_tick_folds_into_the_open_bar(self) -> None:
        bars = MinuteBars()
        bars.add("BTC/USD", 100.0, T0 + timedelta(minutes=1, seconds=1))
        self.assertIsNone(bars.add("BTC/USD", 105.0, T0 + timedelta(seconds=59)))  # older minute
        self.assertEqual(bars.closed("BTC/USD"), [])
        done = bars.add("BTC/USD", 101.0, T0 + timedelta(minutes=2))
        self.assertEqual((done.minute, done.high), (T0 + timedelta(minutes=1), 105.0))

    def test_only_the_newest_bars_are_kept(self) -> None:
        bars = MinuteBars(keep=3)
        for i in range(6):
            bars.add("ETH/USD", 10.0 + i, T0 + timedelta(minutes=i))
        self.assertEqual([b.close for b in bars.closed("ETH/USD")], [12.0, 13.0, 14.0])


class RegimeTests(unittest.TestCase):
    def test_efficiency_ratio(self) -> None:
        self.assertEqual(efficiency_ratio([1, 2, 3, 4]), 1.0)
        self.assertEqual(efficiency_ratio([1, 2, 1, 2, 1]), 0.0)
        self.assertEqual(efficiency_ratio([5]), 0.0)

    def test_too_little_history_is_warming(self) -> None:
        self.assertEqual(read_regime(bars_from([100] * 59)), WARMING)

    def test_a_steady_climb_is_trending(self) -> None:
        self.assertEqual(read_regime(bars_from([100 + i for i in range(100)])), TRENDING)

    def test_back_and_forth_is_choppy(self) -> None:
        self.assertEqual(read_regime(bars_from([100 + (i % 2) for i in range(100)])), CHOPPY)

    def test_two_up_one_down_is_unclear(self) -> None:
        closes, price = [], 100.0
        for i in range(100):
            price += 2 if i % 2 == 0 else -1
            closes.append(price)
        self.assertEqual(read_regime(bars_from(closes)), UNCLEAR)  # ER = 15/45 = 0.33

    def test_calm_after_a_wild_day_is_a_squeeze_even_when_choppy(self) -> None:
        wild = [100 + 2 * (i % 2) for i in range(260)]
        calm = [100 + 0.01 * (i % 2) for i in range(40)]
        self.assertEqual(read_regime(bars_from(wild + calm)), SQUEEZE)
