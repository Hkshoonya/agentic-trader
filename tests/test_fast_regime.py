"""Ticks become minute bars; bars become one of five market readings."""

from __future__ import annotations

import unittest
from datetime import timedelta

from agentic_trading.fast.bars import MinuteBars
from agentic_trading.fast.regime import (
    CHOPPY, SQUEEZE, TRENDING, UNCLEAR, WARMING, efficiency_ratio, history_bars, read_regime, realized_vol,
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

    def test_fifteen_minute_bars_open_on_the_quarter_hour(self) -> None:
        bars = MinuteBars(minutes=15)
        for offset, price in ((timedelta(minutes=3), 100.0), (timedelta(minutes=9), 103.0),
                              (timedelta(minutes=14, seconds=59), 101.0)):
            self.assertIsNone(bars.add("BTC/USD", price, T0 + offset))
        done = bars.add("BTC/USD", 102.0, T0 + timedelta(minutes=15))
        self.assertEqual((done.minute, done.open, done.high, done.close), (T0, 100.0, 103.0, 101.0))

    def test_a_bar_length_that_does_not_divide_the_hour_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "bar minutes"):
            MinuteBars(minutes=7)

    def test_a_day_is_kept_but_never_fewer_bars_than_the_squeeze_needs(self) -> None:
        self.assertEqual([history_bars(m) for m in (1, 5, 15, 60)], [1440, 288, 240, 240])

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


class SpeedTests(unittest.TestCase):
    def test_volatility_matches_the_textbook_and_a_full_day_reads_in_milliseconds(self) -> None:
        import math
        import statistics
        import time

        closes = [100 + 3 * math.sin(i / 7) + (i % 5) * 0.1 for i in range(1440)]
        returns = [math.log(b / a) for a, b in zip(closes, closes[1:])]
        self.assertAlmostEqual(realized_vol(closes[-31:]), statistics.pstdev(returns[-30:]), places=12)
        bars = bars_from(closes)
        best = min(self._time(bars, time) for _ in range(3))
        # The switchboard reads every coin once a minute on the venues event loop,
        # and a 90-day replay does it ~390,000 times: it must take milliseconds.
        self.assertLess(best, 0.010)

    @staticmethod
    def _time(bars, time) -> float:
        start = time.perf_counter()
        read_regime(bars)
        return time.perf_counter() - start
